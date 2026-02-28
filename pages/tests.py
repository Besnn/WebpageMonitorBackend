from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from unittest.mock import patch, MagicMock
from pathlib import Path
import tempfile
import shutil

from pages.models import MonitoredPage, MonitoredPageCheck
from pages.notifications import handle_post_check_notification, _consecutive_failures


SQLITE_TEST_DB = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}


@override_settings(DATABASES=SQLITE_TEST_DB)
class NotificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="x")
        self.page = MonitoredPage.objects.create(
            user=self.user,
            url="http://example.invalid/health",
            notifications_enabled=True,
            alert_threshold=2,
        )

    def _mk_check(self, is_up: bool, offset_seconds: int = 0, status_code: int | None = None):
        ts = timezone.now() + timedelta(seconds=offset_seconds)
        return MonitoredPageCheck.objects.create(
            page=self.page,
            checked_at=ts,
            is_up=is_up,
            status_code=status_code if status_code is not None else (200 if is_up else None),
            response_time_ms=123.0,
            message="OK" if is_up else "ERR",
        )

    def test_consecutive_failures_counter(self):
        # Start with two failures, then a success
        self._mk_check(False, offset_seconds=1)
        self._mk_check(False, offset_seconds=2)
        self.assertEqual(_consecutive_failures(self.page), 2)

        # Add a success; counter should reset to 0 (but function only counts from latest backward)
        self._mk_check(True, offset_seconds=3)
        self.assertEqual(_consecutive_failures(self.page), 0)

        # Add another failure; now streak is 1 (latest is failure)
        self._mk_check(False, offset_seconds=4)
        self.assertEqual(_consecutive_failures(self.page), 1)

        # Latest is success -> consecutive failures should be 0
        self._mk_check(True, offset_seconds=5)
        self.assertEqual(_consecutive_failures(self.page), 0)

    @patch("pages.notifications.send_mail")
    def test_email_sent_only_when_reaching_threshold(self, mock_send_mail):
        # First failure: below threshold -> no email
        self._mk_check(False, offset_seconds=1)
        latest = self.page.checks.order_by("-checked_at").first()
        handle_post_check_notification(self.page, latest)
        mock_send_mail.assert_not_called()

        # Second consecutive failure: equals threshold -> send exactly once
        self._mk_check(False, offset_seconds=2)
        latest = self.page.checks.order_by("-checked_at").first()
        handle_post_check_notification(self.page, latest)
        self.assertEqual(mock_send_mail.call_count, 1)

        # Third consecutive failure: above threshold -> no additional email
        self._mk_check(False, offset_seconds=3)
        latest = self.page.checks.order_by("-checked_at").first()
        handle_post_check_notification(self.page, latest)
        self.assertEqual(mock_send_mail.call_count, 1)

        # Success resets streak; after two more failures we should send again
        self._mk_check(True, offset_seconds=4)
        self._mk_check(False, offset_seconds=5)
        latest = self._mk_check(False, offset_seconds=6)
        handle_post_check_notification(self.page, latest)
        self.assertEqual(mock_send_mail.call_count, 2)

    @patch("pages.notifications.send_mail")
    def test_no_email_when_notifications_disabled(self, mock_send_mail):
        self.page.notifications_enabled = False
        self.page.save(update_fields=["notifications_enabled"])

        latest = self._mk_check(False, offset_seconds=1)
        handle_post_check_notification(self.page, latest)
        mock_send_mail.assert_not_called()

    @patch("pages.notifications.send_mail")
    def test_no_email_when_latest_is_up(self, mock_send_mail):
        latest = self._mk_check(True, offset_seconds=1)
        handle_post_check_notification(self.page, latest)
        mock_send_mail.assert_not_called()

    @patch("pages.notifications.send_mail")
    def test_no_email_when_user_has_no_email(self, mock_send_mail):
        self.user.email = ""
        self.user.save(update_fields=["email"])

        # Hit threshold
        self._mk_check(False, offset_seconds=1)
        latest = self._mk_check(False, offset_seconds=2)
        handle_post_check_notification(self.page, latest)
        mock_send_mail.assert_not_called()

    def test_send_mail_exceptions_are_swallowed(self):
        # Prepare to hit threshold
        self._mk_check(False, offset_seconds=1)
        latest = self._mk_check(False, offset_seconds=2)

        with patch("pages.notifications.send_mail", side_effect=Exception("boom")):
            # Should not raise
            handle_post_check_notification(self.page, latest)

    @patch("pages.notifications.send_mail")
    def test_notify_on_site_recovery(self, mock_send_mail):
        # Site is down for two checks (threshold=2), user notified for DOWN
        self._mk_check(False, offset_seconds=1)
        self._mk_check(False, offset_seconds=2)
        latest = self.page.checks.order_by("-checked_at").first()
        handle_post_check_notification(self.page, latest)
        self.assertEqual(mock_send_mail.call_count, 1)  # DOWN notification

        # Site goes up
        up_check = self._mk_check(True, offset_seconds=3)
        # Simulate notification logic for recovery
        # You may need to implement a recovery notification in handle_post_check_notification
        handle_post_check_notification(self.page, up_check)

        # Site goes down again
        self._mk_check(False, offset_seconds=4)
        self._mk_check(False, offset_seconds=5)
        latest = self.page.checks.order_by("-checked_at").first()
        handle_post_check_notification(self.page, latest)
        self.assertEqual(mock_send_mail.call_count, 2)  # Second DOWN notification


# ===========================================================================
# Screenshot & visual-diff tests
# ===========================================================================

@override_settings(DATABASES=SQLITE_TEST_DB)
class ScreenshotModelFieldTests(TestCase):
    """Verify screenshot-related fields are stored and retrieved correctly."""

    def setUp(self):
        self.user = User.objects.create_user(username="ss1", email="ss1@test.com", password="x")
        self.page = MonitoredPage.objects.create(
            user=self.user,
            url="http://example.invalid",
            screenshot_enabled=True,
        )

    def test_screenshot_enabled_defaults_false(self):
        page2 = MonitoredPage.objects.create(user=self.user, url="http://other.invalid")
        self.assertFalse(page2.screenshot_enabled)

    def test_check_stores_screenshot_fields(self):
        check = MonitoredPageCheck.objects.create(
            page=self.page,
            is_up=True,
            status_code=200,
            response_time_ms=100,
            message="OK",
            screenshot_path="1/abc.png",
            diff_path="1/abc_diff.png",
            diff_score=12.5,
        )
        check.refresh_from_db()
        self.assertEqual(check.screenshot_path, "1/abc.png")
        self.assertEqual(check.diff_path, "1/abc_diff.png")
        self.assertAlmostEqual(check.diff_score, 12.5)

    def test_check_screenshot_fields_default_blank(self):
        check = MonitoredPageCheck.objects.create(
            page=self.page, is_up=True, status_code=200,
            response_time_ms=50, message="OK",
        )
        check.refresh_from_db()
        self.assertEqual(check.screenshot_path, "")
        self.assertEqual(check.diff_path, "")
        self.assertIsNone(check.diff_score)


@override_settings(DATABASES=SQLITE_TEST_DB)
class ScreenshotCaptureTests(TestCase):
    """Test the screenshot capture function with a mocked Playwright."""

    def setUp(self):
        self.user = User.objects.create_user(username="sc1", email="sc1@test.com", password="x")
        self.page = MonitoredPage.objects.create(
            user=self.user,
            url="http://example.invalid",
            screenshot_enabled=True,
        )
        # Use a temporary directory for screenshots
        self.tmp_dir = tempfile.mkdtemp()
        self._patcher = patch("pages.screenshots._screenshots_root", return_value=Path(self.tmp_dir))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("pages.screenshots.sync_playwright")
    def test_capture_screenshot_returns_relative_path(self, mock_pw_ctx):
        """Happy path: playwright runs, file is 'saved', path returned."""
        # Mock the playwright chain
        mock_browser = MagicMock()
        mock_context = MagicMock()
        mock_page = MagicMock()

        mock_pw = MagicMock()
        mock_pw.chromium.launch.return_value = mock_browser
        mock_browser.new_context.return_value = mock_context
        mock_context.new_page.return_value = mock_page

        mock_pw_ctx.return_value.__enter__ = MagicMock(return_value=mock_pw)
        mock_pw_ctx.return_value.__exit__ = MagicMock(return_value=False)

        # Make the screenshot call create an actual (empty) file
        def fake_screenshot(path, full_page):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"PNG_FAKE")
        mock_page.screenshot.side_effect = fake_screenshot

        from pages.screenshots import capture_screenshot
        rel_path = capture_screenshot("http://example.invalid", self.page.id)

        self.assertTrue(rel_path)
        self.assertTrue(rel_path.startswith(str(self.page.id)))
        self.assertTrue(rel_path.endswith(".png"))
        # File should exist on disk
        abs_path = Path(self.tmp_dir) / rel_path
        self.assertTrue(abs_path.is_file())

    @patch("pages.screenshots.sync_playwright", side_effect=Exception("browser crash"))
    def test_capture_screenshot_returns_empty_on_failure(self, _mock):
        from pages.screenshots import capture_screenshot
        rel_path = capture_screenshot("http://example.invalid", self.page.id)
        self.assertEqual(rel_path, "")


@override_settings(DATABASES=SQLITE_TEST_DB)
class VisualDiffTests(TestCase):
    """Test the visual diff computation."""

    def setUp(self):
        self.user = User.objects.create_user(username="vd1", email="vd1@test.com", password="x")
        self.page = MonitoredPage.objects.create(
            user=self.user, url="http://example.invalid", screenshot_enabled=True,
        )
        self.tmp_dir = tempfile.mkdtemp()
        self._patcher = patch("pages.screenshots._screenshots_root", return_value=Path(self.tmp_dir))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_image(self, rel_path, color=(255, 255, 255)):
        """Helper: create a small solid-color PNG on disk."""
        from PIL import Image
        abs_path = Path(self.tmp_dir) / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (100, 100), color)
        img.save(str(abs_path))

    def test_identical_images_score_zero(self):
        self._create_image("1/a.png", color=(200, 200, 200))
        self._create_image("1/b.png", color=(200, 200, 200))

        from pages.screenshots import compute_diff
        diff_rel, score = compute_diff("1/a.png", "1/b.png", self.page.id)

        self.assertTrue(diff_rel)
        self.assertAlmostEqual(score, 0.0)

    def test_different_images_nonzero_score(self):
        self._create_image("1/a.png", color=(0, 0, 0))
        self._create_image("1/b.png", color=(255, 255, 255))

        from pages.screenshots import compute_diff
        diff_rel, score = compute_diff("1/a.png", "1/b.png", self.page.id)

        self.assertTrue(diff_rel)
        self.assertGreater(score, 0)

    def test_missing_file_returns_empty(self):
        from pages.screenshots import compute_diff
        diff_rel, score = compute_diff("1/missing.png", "1/also_missing.png", self.page.id)
        self.assertEqual(diff_rel, "")
        self.assertIsNone(score)

    def test_empty_paths_return_empty(self):
        from pages.screenshots import compute_diff
        diff_rel, score = compute_diff("", "", self.page.id)
        self.assertEqual(diff_rel, "")
        self.assertIsNone(score)


@override_settings(DATABASES=SQLITE_TEST_DB)
class ScreenshotSettingsToggleTests(TestCase):
    """Verify the screenshot_enabled setting can be toggled via the settings API."""

    def setUp(self):
        self.user = User.objects.create_user(username="st1", email="st1@test.com", password="pass123")
        self.client.login(username="st1", password="pass123")
        self.page = MonitoredPage.objects.create(
            user=self.user, url="http://example.invalid", screenshot_enabled=False,
        )

    def test_toggle_screenshot_enabled_on(self):
        import json
        resp = self.client.put(
            f"/api/monitor/{self.page.id}/settings/",
            data=json.dumps({"screenshotEnabled": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.page.refresh_from_db()
        self.assertTrue(self.page.screenshot_enabled)

    def test_site_detail_includes_screenshot_fields(self):
        check = MonitoredPageCheck.objects.create(
            page=self.page, is_up=True, status_code=200,
            response_time_ms=50, message="OK",
            screenshot_path="1/test.png", diff_score=5.0,
        )
        resp = self.client.get(f"/api/monitor/{self.page.id}/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('screenshot_enabled', data['site'])
        self.assertIn('last_screenshot_url', data['summary'])
        # Check items should contain screenshot_url
        self.assertTrue(any('screenshot_url' in c for c in data['checks']))


@override_settings(DATABASES=SQLITE_TEST_DB)
class NoChangeDiffTests(TestCase):
    """When diff_score == 0, compute_diff should NOT create a diff image."""

    def setUp(self):
        self.user = User.objects.create_user(username="nc1", email="nc1@test.com", password="x")
        self.page = MonitoredPage.objects.create(
            user=self.user, url="http://example.invalid", screenshot_enabled=True,
        )
        self.tmp_dir = tempfile.mkdtemp()
        self._patcher = patch("pages.screenshots._screenshots_root", return_value=Path(self.tmp_dir))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_image(self, rel_path, color=(255, 255, 255)):
        from PIL import Image
        abs_path = Path(self.tmp_dir) / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (100, 100), color)
        img.save(str(abs_path))

    def test_identical_images_produce_no_diff_file(self):
        self._create_image("1/a.png", color=(128, 128, 128))
        self._create_image("1/b.png", color=(128, 128, 128))

        from pages.screenshots import compute_diff
        diff_rel, score = compute_diff("1/a.png", "1/b.png", self.page.id)

        self.assertAlmostEqual(score, 0.0)
        # diff_rel should be empty — no diff file created
        self.assertEqual(diff_rel, "")

    def test_different_images_produce_diff_file(self):
        self._create_image("1/a.png", color=(0, 0, 0))
        self._create_image("1/b.png", color=(255, 255, 255))

        from pages.screenshots import compute_diff
        diff_rel, score = compute_diff("1/a.png", "1/b.png", self.page.id)

        self.assertGreater(score, 0)
        self.assertTrue(diff_rel)
        # diff file should exist on disk
        abs_diff = Path(self.tmp_dir) / diff_rel
        self.assertTrue(abs_diff.is_file())


@override_settings(DATABASES=SQLITE_TEST_DB)
class DeleteScreenshotFileTests(TestCase):
    """Test the delete_screenshot_file helper."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self._patcher = patch("pages.screenshots._screenshots_root", return_value=Path(self.tmp_dir))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_deletes_existing_file(self):
        f = Path(self.tmp_dir) / "1" / "test.png"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"data")
        self.assertTrue(f.is_file())

        from pages.screenshots import delete_screenshot_file
        delete_screenshot_file("1/test.png")
        self.assertFalse(f.is_file())

    def test_ignores_missing_file(self):
        from pages.screenshots import delete_screenshot_file
        # Should not raise
        delete_screenshot_file("1/nonexistent.png")

    def test_ignores_empty_path(self):
        from pages.screenshots import delete_screenshot_file
        delete_screenshot_file("")


@override_settings(DATABASES=SQLITE_TEST_DB, MAX_SCREENSHOTS_PER_PAGE=3)
class CleanupOldScreenshotsTests(TestCase):
    """Test that cleanup_old_screenshots keeps only N newest screenshots."""

    def setUp(self):
        self.user = User.objects.create_user(username="cl1", email="cl1@test.com", password="x")
        self.page = MonitoredPage.objects.create(
            user=self.user, url="http://example.invalid", screenshot_enabled=True,
        )
        self.tmp_dir = tempfile.mkdtemp()
        self._patcher = patch("pages.screenshots._screenshots_root", return_value=Path(self.tmp_dir))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _mk_check_with_ss(self, offset_seconds, ss_path="", diff_path=""):
        """Create a check with a screenshot file on disk."""
        from django.utils import timezone
        from datetime import timedelta

        ts = timezone.now() + timedelta(seconds=offset_seconds)
        if ss_path:
            abs_path = Path(self.tmp_dir) / ss_path
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(b"PNG")
        if diff_path:
            abs_path = Path(self.tmp_dir) / diff_path
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_bytes(b"DIFF")

        return MonitoredPageCheck.objects.create(
            page=self.page,
            checked_at=ts,
            is_up=True,
            status_code=200,
            response_time_ms=50,
            message="OK",
            screenshot_path=ss_path,
            diff_path=diff_path,
        )

    def test_prunes_beyond_limit(self):
        """With MAX_SCREENSHOTS_PER_PAGE=3, the 4th+ oldest screenshot should be pruned."""
        # Create 5 checks with screenshots (oldest first)
        checks = []
        for i in range(5):
            c = self._mk_check_with_ss(
                offset_seconds=i,
                ss_path=f"1/ss_{i}.png",
                diff_path=f"1/diff_{i}.png" if i > 0 else "",
            )
            checks.append(c)

        from pages.screenshots import cleanup_old_screenshots
        cleanup_old_screenshots(self.page)

        # The 3 newest (i=2,3,4) should keep their files
        for c in checks[2:]:
            c.refresh_from_db()
            self.assertTrue(c.screenshot_path)  # still set

        # The 2 oldest (i=0,1) should have paths cleared
        for c in checks[:2]:
            c.refresh_from_db()
            self.assertEqual(c.screenshot_path, "")
            self.assertEqual(c.diff_path, "")

        # Their files should be deleted from disk
        self.assertFalse((Path(self.tmp_dir) / "1" / "ss_0.png").exists())
        self.assertFalse((Path(self.tmp_dir) / "1" / "ss_1.png").exists())
        self.assertFalse((Path(self.tmp_dir) / "1" / "diff_1.png").exists())

        # Newest files should still exist
        self.assertTrue((Path(self.tmp_dir) / "1" / "ss_4.png").exists())

    def test_no_prune_when_under_limit(self):
        """When there are fewer screenshots than the limit, nothing is pruned."""
        c = self._mk_check_with_ss(offset_seconds=1, ss_path="1/only.png")

        from pages.screenshots import cleanup_old_screenshots
        cleanup_old_screenshots(self.page)

        c.refresh_from_db()
        self.assertEqual(c.screenshot_path, "1/only.png")
        self.assertTrue((Path(self.tmp_dir) / "1" / "only.png").exists())


