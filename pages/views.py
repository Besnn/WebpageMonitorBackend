from django.conf import settings
from django.http import HttpResponse, JsonResponse, FileResponse, Http404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from datetime import timedelta
from pathlib import Path

import json
import ssl
import time
import urllib.request
import urllib.error

from .models import MonitoredPage, MonitoredPageCheck
from .notifications import handle_post_check_notification, handle_change_notification
from .screenshots import capture_screenshot, compute_diff, _screenshots_root, delete_screenshot_file, cleanup_old_screenshots, get_or_create_thumbnail, create_thumbnail, _thumb_rel
from .storage import screenshot_storage as _storage

# Create your views here.

def homePageView(request):
    return HttpResponse("Hello, World")

@csrf_exempt
@require_http_methods(["GET", "POST"])
def monitor(request):
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return JsonResponse({'error': 'Not authenticated'}, status=401)

    if request.method == 'GET':
        import threading
        pages = MonitoredPage.objects.filter(user=user).order_by('-created_at')
        page_list = []
        for page in pages:
            # Most-recent check with a screenshot (for thumbnail)
            last_check_with_ss = page.checks.exclude(screenshot_path='').order_by('-checked_at').first()
            # Most-recent check of any kind (for is_up status)
            last_check_any = page.checks.order_by('-checked_at').first()

            thumbnail_url = ''
            screenshot_missing = False
            if last_check_with_ss and last_check_with_ss.screenshot_path:
                source_rel = last_check_with_ss.screenshot_path
                # Only build a URL if the source file actually exists in storage.
                # If the screenshots directory is missing or the file was deleted,
                # set screenshot_missing so the frontend shows "no image" instead
                # of "capturing…".
                if _storage.exists(source_rel):
                    thumb_rel = get_or_create_thumbnail(source_rel, page.id)
                    if thumb_rel:
                        thumbnail_url = f'/api/screenshots/{thumb_rel}'
                    else:
                        thumbnail_url = f'/api/screenshots/{source_rel}'
                else:
                    screenshot_missing = True
            elif last_check_any is None or last_check_any.is_up:
                # No screenshot yet but site appears up — trigger background capture
                def _bg_capture(p=page):
                    try:
                        _perform_single_check(p, force_screenshot=True)
                    except Exception:
                        pass
                threading.Thread(target=_bg_capture, daemon=True).start()

            # None  = never checked, True = up, False = down
            is_up = last_check_any.is_up if last_check_any else None

            page_list.append({
                'id': page.id,
                'url': page.url,
                'created_at': page.created_at.isoformat(),
                'last_screenshot_url': thumbnail_url,
                'is_up': is_up,
                'is_pinned': page.is_pinned,
                'screenshot_missing': screenshot_missing,
            })
        return JsonResponse({'pages': page_list}, status=200)

    try:
        data = json.loads(request.body.decode('utf-8'))
        url = (data.get('webpageURL') or '').strip()
        if not url:
            return JsonResponse({'error': 'webpageURL is required'}, status=400)

        page, created = MonitoredPage.objects.get_or_create(user=user, url=url)

        # If this is a newly created page, immediately perform a check so the UI
        # has fresh status, and always capture an initial screenshot for the thumbnail.
        if created:
            _perform_single_check(page, force_screenshot=True)

        return JsonResponse(
            {
                'page': {
                    'id': str(page.id),
                    'url': page.url,
                    'created_at': page.created_at.isoformat(),
                },
                'created': created,
            },
            status=201 if created else 200,
        )
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON format'}, status=400)


def _perform_single_check(page, timeout_seconds: int = 10, force_screenshot: bool = False) -> None:
    """Perform a single HTTP check for the given MonitoredPage and store the result.

    When *force_screenshot* is True a screenshot is captured even if
    ``page.screenshot_enabled`` is False (used for the initial thumbnail on add).
    """
    started_at = time.perf_counter()
    status_code = None
    is_up = False
    message = ""

    try:
        request = urllib.request.Request(
            page.url,
            headers={"User-Agent": "WebpageMonitor/1.0"},
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(request, timeout=max(1, int(timeout_seconds)), context=ctx) as response:
            status_code = response.getcode()
            is_up = 200 <= status_code < 400
            message = "OK" if is_up else f"Status {status_code}"
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        is_up = False
        message = f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        status_code = None
        is_up = False
        message = f"Error: {getattr(exc, 'reason', exc)}"
    except Exception as exc:  # pragma: no cover - defensive fallback
        status_code = None
        is_up = False
        message = f"Error: {exc}"

    elapsed_ms = (time.perf_counter() - started_at) * 1000

    # --- Screenshot capture & visual diff ---
    screenshot_rel = ""
    crop_rel = ""
    diff_rel = ""
    diff_score = None
    if (page.screenshot_enabled or force_screenshot) and is_up:
        try:
            screenshot_rel, crop_rel = capture_screenshot(page.url, page.id)
            # For diffing, prefer the cropped version when available
            diff_source = crop_rel or screenshot_rel
            prev_check = page.checks.order_by('-checked_at').first()
            if screenshot_rel and prev_check and prev_check.screenshot_path:
                # Use previous crop if it exists, otherwise previous full screenshot
                prev_diff_source = prev_check.crop_path or prev_check.screenshot_path
                diff_rel, diff_score = compute_diff(
                    prev_diff_source, diff_source, page.id
                )
                # If nothing changed, discard the new screenshot and
                # reuse the previous one so we don't waste disk space.
                if diff_score is not None and diff_score == 0:
                    delete_screenshot_file(screenshot_rel)
                    if crop_rel:
                        delete_screenshot_file(crop_rel)
                    screenshot_rel = prev_check.screenshot_path
                    crop_rel = prev_check.crop_path
        except Exception:
            crop_rel = ""

    # Store the check result
    latest = MonitoredPageCheck.objects.create(
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

    # Prune old screenshots beyond the retention limit
    if screenshot_rel:
        try:
            cleanup_old_screenshots(page)
        except Exception:
            pass

    # Fire notification logic (do not let it raise)
    try:
        handle_post_check_notification(page, latest)
    except Exception:
        pass

    # Fire visual-change notification (do not let it raise)
    try:
        handle_change_notification(page, latest)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helper: build a check dict with optional screenshot URLs
# ---------------------------------------------------------------------------

def _check_to_dict(check, request=None):
    """Serialise a MonitoredPageCheck to a dict, including screenshot URLs."""
    d = {
        'id': str(check.id),
        'checked_at': check.checked_at.isoformat(),
        'status_code': check.status_code,
        'response_time_ms': check.response_time_ms,
        'is_up': check.is_up,
        'message': check.message,
        'screenshot_url': '',
        'diff_url': '',
        'diff_score': check.diff_score,
    }
    if check.screenshot_path and _storage.exists(check.screenshot_path):
        d['screenshot_url'] = f"/api/screenshots/{check.screenshot_path}"
    if check.diff_path and _storage.exists(check.diff_path):
        d['diff_url'] = f"/api/screenshots/{check.diff_path}"
    return d


@csrf_exempt
@require_http_methods(["DELETE"])
def monitor_site_delete(request, site_id):
    """Delete a monitored site and all its screenshot artefacts."""
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return JsonResponse({'error': 'Not authenticated'}, status=401)
    try:
        page = MonitoredPage.objects.get(pk=site_id, user=user)
    except MonitoredPage.DoesNotExist:
        return JsonResponse({'error': 'Site not found'}, status=404)

    # Delete all screenshot artefacts (full, crop, diff, thumbnail) from storage
    for check in page.checks.all():
        delete_screenshot_file(check.screenshot_path)
        delete_screenshot_file(check.crop_path)
        delete_screenshot_file(check.diff_path)
        if check.screenshot_path:
            delete_screenshot_file(_thumb_rel(check.screenshot_path))

    page.delete()
    return JsonResponse({'deleted': True}, status=200)


@csrf_exempt
@require_http_methods(["PATCH"])
def monitor_site_pin(request, site_id):
    """Toggle the pinned state of a monitored site."""
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return JsonResponse({'error': 'Not authenticated'}, status=401)
    try:
        page = MonitoredPage.objects.get(pk=site_id, user=user)
    except MonitoredPage.DoesNotExist:
        return JsonResponse({'error': 'Site not found'}, status=404)

    try:
        data = json.loads(request.body.decode('utf-8'))
        pinned = bool(data.get('is_pinned', not page.is_pinned))
    except (json.JSONDecodeError, KeyError):
        pinned = not page.is_pinned

    page.is_pinned = pinned
    page.save(update_fields=['is_pinned'])
    return JsonResponse({'id': page.id, 'is_pinned': page.is_pinned}, status=200)


@require_http_methods(["GET"])
def monitor_site_detail(request, site_id):
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return JsonResponse({'error': 'Not authenticated'}, status=401)

    try:
        page = MonitoredPage.objects.get(pk=site_id, user=user)
    except MonitoredPage.DoesNotExist:
        return JsonResponse({'error': 'Site not found'}, status=404)

    latest_check = page.checks.order_by('-checked_at').first()
    limit = getattr(settings, 'RECENT_CHECKS_LIMIT', 10)
    checks = page.checks.order_by('-checked_at')[:limit]

    check_items = [_check_to_dict(c, request) for c in checks]

    # Calculate uptime percentage from recent checks
    total_checks = len(check_items)
    up_checks = sum(1 for check in check_items if check['is_up'])
    uptime_percent = (up_checks / total_checks * 100) if total_checks > 0 else None

    summary = {
        'current_status': 'UP' if latest_check and latest_check.is_up else 'DOWN',
        'last_checked_at': latest_check.checked_at.isoformat() if latest_check else None,
        'last_status_code': latest_check.status_code if latest_check else None,
        'last_response_time_ms': latest_check.response_time_ms if latest_check else None,
        'uptime_percent': round(uptime_percent, 2) if uptime_percent is not None else None,
        'last_screenshot_url': '',
        'last_diff_url': '',
        'last_diff_score': None,
    }
    if latest_check:
        if latest_check.screenshot_path and _storage.exists(latest_check.screenshot_path):
            summary['last_screenshot_url'] = f"/api/screenshots/{latest_check.screenshot_path}"
        if latest_check.diff_path and _storage.exists(latest_check.diff_path):
            summary['last_diff_url'] = f"/api/screenshots/{latest_check.diff_path}"
        summary['last_diff_score'] = latest_check.diff_score

    return JsonResponse(
        {
            'site': {
                'id': str(page.id),
                'url': page.url,
                'created_at': page.created_at.isoformat(),
                'check_interval': page.check_interval,
                'notifications_enabled': page.notifications_enabled,
                'alert_threshold': page.alert_threshold,
                'screenshot_enabled': page.screenshot_enabled,
                'change_notifications_enabled': page.change_notifications_enabled,
                'region_left_pct': page.region_left_pct,
                'region_top_pct': page.region_top_pct,
                'region_width_pct': page.region_width_pct,
                'region_height_pct': page.region_height_pct,
            },
            'summary': summary,
            'checks': check_items,
        },
        status=200,
    )


@require_http_methods(["GET"])
def monitor_site_history(request, site_id):
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return JsonResponse({'error': 'Not authenticated'}, status=401)

    try:
        page = MonitoredPage.objects.get(pk=site_id, user=user)
    except MonitoredPage.DoesNotExist:
        return JsonResponse({'error': 'Site not found'}, status=404)

    hours = request.GET.get('hours')
    try:
        hours = int(hours) if hours else 24
    except ValueError:
        hours = 24

    since = timezone.now() - timedelta(hours=hours)
    checks = page.checks.filter(checked_at__gte=since).order_by('checked_at')

    history_items = [
        {
            'checked_at': check.checked_at.isoformat(),
            'response_time_ms': check.response_time_ms,
            'status_code': check.status_code,
            'is_up': check.is_up,
            'diff_score': check.diff_score,
        }
        for check in checks
    ]

    return JsonResponse({'history': history_items}, status=200)


@csrf_exempt
@require_http_methods(["PUT", "PATCH"])
def monitor_site_settings(request, site_id):
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return JsonResponse({'error': 'Not authenticated'}, status=401)

    try:
        page = MonitoredPage.objects.get(pk=site_id, user=user)
    except MonitoredPage.DoesNotExist:
        return JsonResponse({'error': 'Site not found'}, status=404)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON format'}, status=400)

    # Update URL if provided
    if 'url' in data:
        url = data['url'].strip()
        if url:
            page.url = url

    # Update check_interval if provided
    if 'checkInterval' in data:
        try:
            check_interval = int(data['checkInterval'])
            if 1 <= check_interval <= 60:
                page.check_interval = check_interval
            else:
                return JsonResponse({'error': 'Check interval must be between 1 and 60 minutes'}, status=400)
        except (ValueError, TypeError):
            return JsonResponse({'error': 'Invalid check interval value'}, status=400)

    # Update notifications_enabled if provided
    if 'notificationsEnabled' in data:
        page.notifications_enabled = bool(data['notificationsEnabled'])

    # Update alert_threshold if provided
    if 'alertThreshold' in data:
        try:
            alert_threshold = int(data['alertThreshold'])
            if 1 <= alert_threshold <= 10:
                page.alert_threshold = alert_threshold
            else:
                return JsonResponse({'error': 'Alert threshold must be between 1 and 10'}, status=400)
        except (ValueError, TypeError):
            return JsonResponse({'error': 'Invalid alert threshold value'}, status=400)

    # Update screenshot_enabled if provided
    if 'screenshotEnabled' in data:
        page.screenshot_enabled = bool(data['screenshotEnabled'])

    # Update change_notifications_enabled if provided
    if 'changeNotificationsEnabled' in data:
        page.change_notifications_enabled = bool(data['changeNotificationsEnabled'])

    # Update region settings if provided
    region_fields = [
        ('regionLeftPct', 'region_left_pct'),
        ('regionTopPct', 'region_top_pct'),
        ('regionWidthPct', 'region_width_pct'),
        ('regionHeightPct', 'region_height_pct'),
    ]
    for frontend_key, model_attr in region_fields:
        if frontend_key in data:
            try:
                val = float(data[frontend_key])
                if 0 <= val <= 1:
                    setattr(page, model_attr, val)
                else:
                    return JsonResponse({'error': f'{frontend_key} must be between 0 and 1'}, status=400)
            except (ValueError, TypeError):
                return JsonResponse({'error': f'Invalid {frontend_key} value'}, status=400)
 
    page.save()

    return JsonResponse(
        {
            'success': True,
            'site': {
                'id': str(page.id),
                'url': page.url,
                'created_at': page.created_at.isoformat(),
                'check_interval': page.check_interval,
                'notifications_enabled': page.notifications_enabled,
                'alert_threshold': page.alert_threshold,
                'screenshot_enabled': page.screenshot_enabled,
                'change_notifications_enabled': page.change_notifications_enabled,
                'region_left_pct': page.region_left_pct,
                'region_top_pct': page.region_top_pct,
                'region_width_pct': page.region_width_pct,
                'region_height_pct': page.region_height_pct,
            },
        },
        status=200,
    )


# ---------------------------------------------------------------------------
# Screenshot file serving
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
def serve_screenshot(request, path):
    """Serve a screenshot artefact.

    - Local storage   : streams the file directly from disk.
    - SeaweedFS storage: streams the object from SeaweedFS through Django
      (the browser never contacts SeaweedFS directly).
    - S3 storage      : returns a 302 redirect to a short-lived pre-signed URL
      so the browser fetches the image directly from S3.

    Access control: the requesting user must own the page the screenshot belongs
    to (page_id is the first component of the path).
    """
    from django.http import HttpResponseRedirect
    from .storage import screenshot_storage as _storage

    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return JsonResponse({'error': 'Not authenticated'}, status=401)

    safe = Path(path)
    if '..' in safe.parts:
        raise Http404

    try:
        page_id = int(safe.parts[0])
    except (IndexError, ValueError):
        raise Http404

    if not MonitoredPage.objects.filter(pk=page_id, user=user).exists():
        return JsonResponse({'error': 'Forbidden'}, status=403)

    rel = str(safe)

    if not _storage.exists(rel):
        raise Http404

    content_type = 'image/jpeg' if rel.lower().endswith('.jpg') else 'image/png'

    # S3: redirect to a pre-signed URL (browser fetches directly from S3)
    if getattr(settings, 'USE_S3_STORAGE', False):
        presigned = _storage.url(rel)
        if not presigned:
            raise Http404
        return HttpResponseRedirect(presigned)

    # SeaweedFS: stream the object through Django via storage.open()
    if getattr(settings, 'USE_SEAWEEDFS_STORAGE', False):
        try:
            body = _storage.open(rel)
            return HttpResponse(body.read(), content_type=content_type)
        except Exception:
            raise Http404

    # Local disk: stream directly from the filesystem
    local_path = _storage.local_path(rel)
    if local_path is None or not local_path.is_file():
        raise Http404

    content_type = 'image/jpeg' if rel.lower().endswith('.jpg') else 'image/png'
    return FileResponse(open(local_path, 'rb'), content_type=content_type)

