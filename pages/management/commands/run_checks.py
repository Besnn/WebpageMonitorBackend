import asyncio
import ssl
import time
import urllib.request
import urllib.error
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand
from django.utils import timezone

from pages.models import MonitoredPage, MonitoredPageCheck
from pages.notifications import handle_post_check_notification, handle_change_notification
from pages.screenshots import capture_screenshot, compute_diff, delete_screenshot_file, cleanup_old_screenshots

DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_INTERVAL_SECONDS = 60


class Command(BaseCommand):
    help = "Periodically check monitored pages and record status/latency."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=DEFAULT_INTERVAL_SECONDS,
            help="Seconds between check rounds (how often to scan for sites to check).",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=DEFAULT_TIMEOUT_SECONDS,
            help="Request timeout in seconds.",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single check round and exit.",
        )

    def handle(self, *args, **options):
        interval = max(1, int(options["interval"]))
        timeout = max(1, int(options["timeout"]))
        run_once = options["once"]

        self.stdout.write(self.style.SUCCESS("Starting monitor checks"))
        self.stdout.write(f"Scan interval: {interval}s (checks sites based on their individual check_interval)")

        while True:
            asyncio.run(self._run_checks(timeout=timeout))
            if run_once:
                break
            time.sleep(interval)

    async def _run_single_check(self, page, timeout):
        """Perform a single check for a given page. This is the async core."""
        started_at = time.perf_counter()
        status_code = None
        is_up = False
        message = ""

        # Synchronous network call in a thread to avoid blocking the event loop
        def check_url():
            try:
                request = urllib.request.Request(page.url, headers={"User-Agent": "WebpageMonitor/1.0"})
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(request, timeout=timeout, context=ctx) as response:
                    return response.getcode(), 200 <= response.getcode() < 400, f"Status {response.getcode()}"
            except urllib.error.HTTPError as exc:
                return exc.code, False, f"HTTP {exc.code}"
            except urllib.error.URLError as exc:
                return None, False, f"Error: {getattr(exc, 'reason', exc)}"
            except Exception as exc:
                return None, False, f"Error: {exc}"

        status_code, is_up, message = await asyncio.to_thread(check_url)
        elapsed_ms = (time.perf_counter() - started_at) * 1000

        # --- Screenshot capture & visual diff ---
        screenshot_rel, crop_rel, diff_rel, diff_score = "", "", "", None
        if page.screenshot_enabled and is_up:
            try:
                last_check = await sync_to_async(page.checks.order_by('-checked_at').first)()
                screenshot_rel, crop_rel = await asyncio.to_thread(capture_screenshot, page.url, page.id)
                # For diffing, prefer the cropped version when available
                diff_source = crop_rel or screenshot_rel
                if screenshot_rel and last_check and last_check.screenshot_path:
                    # Use previous crop if it exists, otherwise previous full screenshot
                    prev_diff_source = last_check.crop_path or last_check.screenshot_path
                    diff_rel, diff_score = await asyncio.to_thread(
                        compute_diff, prev_diff_source, diff_source, page.id
                    )
                    if diff_score is not None and diff_score == 0:
                        delete_screenshot_file(screenshot_rel)
                        if crop_rel:
                            delete_screenshot_file(crop_rel)
                        screenshot_rel = last_check.screenshot_path
                        crop_rel = last_check.crop_path
            except Exception:
                pass  # never break the checker

        latest = await sync_to_async(MonitoredPageCheck.objects.create)(
            page=page,
            checked_at=timezone.now(),
            status_code=status_code,
            response_time_ms=round(elapsed_ms, 2),
            is_up=is_up,
            message=message,
            screenshot_path=screenshot_rel,
            crop_path=crop_rel,
            diff_path=diff_rel,
            diff_score=diff_score,
        )

        if screenshot_rel:
            await sync_to_async(cleanup_old_screenshots)(page)

        await sync_to_async(handle_post_check_notification)(page, latest)
        await sync_to_async(handle_change_notification)(page, latest)

        ss_tag = " [+screenshot]" if screenshot_rel else ""
        diff_tag = f" [diff={diff_score:.1f}%]" if diff_score is not None else ""
        self.stdout.write(
            f"Checked {page.url} (interval: {page.check_interval}m) -> "
            f"{status_code or 'ERR'} in {elapsed_ms:.2f}ms{ss_tag}{diff_tag}"
        )

    async def _run_checks(self, timeout):
        pages = await sync_to_async(list)(MonitoredPage.objects.all())
        if not pages:
            self.stdout.write("No monitored pages to check.")
            return

        now = timezone.now()
        tasks = []
        for page in pages:
            last_check = await sync_to_async(page.checks.order_by('-checked_at').first)()
            should_check = False
            if last_check is None:
                should_check = True
            else:
                time_since_last_check = now - last_check.checked_at
                if time_since_last_check >= timedelta(minutes=page.check_interval):
                    should_check = True

            if should_check:
                tasks.append(self._run_single_check(page, timeout))

        if tasks:
            await asyncio.gather(*tasks)
            self.stdout.write(self.style.SUCCESS(f"Checked {len(tasks)} site(s) this round."))
        else:
            self.stdout.write("No sites due for checking this round.")
