from django.db import models
from django.conf import settings

# Create your models here.

class MonitoredPage(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='monitored_pages')
    url = models.URLField(max_length=2048)
    created_at = models.DateTimeField(auto_now_add=True)

    # Settings fields
    check_interval = models.IntegerField(default=5, help_text="Check interval in minutes (1-60)")
    notifications_enabled = models.BooleanField(default=False, help_text="Enable notifications for this site")
    alert_threshold = models.IntegerField(default=3, help_text="Number of consecutive failures before alerting (1-10)")

    # Screenshot settings
    screenshot_enabled = models.BooleanField(default=False, help_text="Capture a screenshot on each check")
    change_notifications_enabled = models.BooleanField(
        default=False,
        help_text="Enable notifications for visual changes",
    )
    region_left_pct = models.FloatField(default=0.0, help_text="Monitored region left position as fraction (0.0-1.0)")
    region_top_pct = models.FloatField(default=0.0, help_text="Monitored region top position as fraction (0.0-1.0)")
    region_width_pct = models.FloatField(default=1.0, help_text="Monitored region width as fraction (0.0-1.0)")
    region_height_pct = models.FloatField(default=1.0, help_text="Monitored region height as fraction (0.0-1.0)")

    is_pinned = models.BooleanField(default=False, help_text="Pinned sites stay at the top of the dashboard")

    class Meta:
        ordering = ['-is_pinned', '-created_at']

    def __str__(self):
        return f"{self.user_id}: {self.url}"


class MonitoredPageCheck(models.Model):
    page = models.ForeignKey(MonitoredPage, on_delete=models.CASCADE, related_name='checks')
    checked_at = models.DateTimeField(auto_now_add=True)
    status_code = models.IntegerField(null=True, blank=True)
    response_time_ms = models.FloatField(null=True, blank=True)
    is_up = models.BooleanField(default=False)
    message = models.CharField(max_length=255, blank=True)

    # Screenshot & visual diff fields (paths relative to SCREENSHOTS_DIR)
    screenshot_path = models.CharField(max_length=512, blank=True, default='')
    crop_path = models.CharField(max_length=512, blank=True, default='',
                                 help_text='Region-cropped screenshot used for diffing (empty when full-page)')
    diff_path = models.CharField(max_length=512, blank=True, default='')
    diff_score = models.FloatField(
        null=True, blank=True,
        help_text="Visual change score 0-100. 0 = identical, 100 = completely different.",
    )

    class Meta:
        ordering = ['-checked_at']
        indexes = [
            models.Index(fields=['page', 'checked_at'], name='pages_page_checked_idx'),
        ]

    def __str__(self):
        status = self.status_code if self.status_code is not None else 'ERR'
        return f"{self.page_id} @ {self.checked_at.isoformat()} ({status})"
