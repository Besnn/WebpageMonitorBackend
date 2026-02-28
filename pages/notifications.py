import logging
from pathlib import Path

from django.core.mail import send_mail, EmailMultiAlternatives
from django.conf import settings
from email.mime.image import MIMEImage

logger = logging.getLogger(__name__)


def _send_plain(subject: str, body: str, recipient: str) -> None:
    """Send a simple plain-text email and log the outcome."""
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'webmon@localhost')
    host = getattr(settings, 'EMAIL_HOST', 'localhost')
    port = getattr(settings, 'EMAIL_PORT', 25)
    logger.info("Sending email to %s via %s:%s | subject: %s", recipient, host, port, subject)
    try:
        send_mail(subject, body, from_email, [recipient], fail_silently=False)
        logger.info("Email sent successfully to %s", recipient)
    except Exception as exc:
        logger.error("Failed to send email to %s (SMTP %s:%s): %s", recipient, host, port, exc)


def _send_html(subject: str, text_body: str, html_body: str, recipient: str,
               inline_images: list[tuple[str, str]] | None = None) -> None:
    """Send a multipart email (text + html) with optional inline images.

    inline_images is a list of (filesystem_path, content_id) tuples.
    Reference them in the HTML as <img src="cid:CONTENT_ID">.
    """
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'webmon@localhost')
    host = getattr(settings, 'EMAIL_HOST', 'localhost')
    port = getattr(settings, 'EMAIL_PORT', 25)
    logger.info("Sending HTML email to %s via %s:%s | subject: %s", recipient, host, port, subject)
    try:
        msg = EmailMultiAlternatives(subject, text_body, from_email, [recipient])
        msg.attach_alternative(html_body, "text/html")

        for fs_path, cid in (inline_images or []):
            try:
                img_path = Path(fs_path)
                if img_path.is_file():
                    with img_path.open('rb') as f:
                        img = MIMEImage(f.read())
                    img.add_header('Content-ID', f'<{cid}>')
                    img.add_header('Content-Disposition', 'inline', filename=img_path.name)
                    msg.attach(img)
                else:
                    logger.warning("Inline image not found: %s", fs_path)
            except Exception as exc:
                logger.warning("Failed to attach inline image %s: %s", fs_path, exc)

        msg.send(fail_silently=False)
        logger.info("HTML email sent successfully to %s", recipient)
    except Exception as exc:
        logger.error("Failed to send HTML email to %s (SMTP %s:%s): %s", recipient, host, port, exc)


def _score_color(score: float) -> str:
    """Return a blue-scale hex colour for the diff score badge."""
    MAJOR_CHANGE_THRESHOLD = 20.0
    MODERATE_CHANGE_THRESHOLD = 5.0
    DARK_BLUE_SCORE_COLOR = "#1e40af"
    MID_BLUE_SCORE_COLOR = "#2563eb"
    LIGHT_BLUE_SCORE_COLOR = "#3b82f6"
    if score >= MAJOR_CHANGE_THRESHOLD:
        return DARK_BLUE_SCORE_COLOR   # dark blue  — large change
    if score >= MODERATE_CHANGE_THRESHOLD:
        return MID_BLUE_SCORE_COLOR   # mid blue   — moderate change
    return LIGHT_BLUE_SCORE_COLOR       # light blue — small change


def handle_change_notification(page, latest_check) -> None:
    """Send a polished HTML email when a visual change is detected on the page."""
    logger.debug("handle_change_notification v2 (white-blue theme)")
    if not getattr(page, 'change_notifications_enabled', False):
        return
    if not getattr(page, 'screenshot_enabled', False):
        return

    diff_score = getattr(latest_check, 'diff_score', None)
    if diff_score is None or diff_score <= 0:
        return

    user = getattr(page, 'user', None)
    recipient = getattr(user, 'email', None)
    if not recipient:
        logger.warning(
            "Visual change notification skipped for page %s: user has no email address", page.url
        )
        return

    site_url = getattr(settings, 'SITE_BASE_URL', 'http://localhost:8000').rstrip('/')
    monitor_url = f"{site_url}/monitor/{page.id}"

    diff_rel      = getattr(latest_check, 'diff_path',       '') or ''
    screenshot_rel = getattr(latest_check, 'screenshot_path', '') or ''
    diff_url      = f"{site_url}/api/screenshots/{diff_rel}"       if diff_rel       else ''
    screenshot_url = f"{site_url}/api/screenshots/{screenshot_rel}" if screenshot_rel else ''

    checked_at_str = latest_check.checked_at.strftime('%B %d, %Y at %H:%M UTC')
    score_color    = _score_color(diff_score)

    # ------------------------------------------------------------------
    # Resolve image files on disk and build CID tags
    # ------------------------------------------------------------------
    screenshots_root = Path(getattr(settings, 'SCREENSHOTS_DIR',
                                    Path(settings.BASE_DIR) / 'screenshots'))
    inline_images: list[tuple[str, str]] = []   # (fs_path, cid)

    def _resolve(rel: str, label: str) -> tuple[str, str]:
        """Return (img_tag, cid) — cid is '' when the file is not on disk."""
        if not rel:
            return ('', '')
        candidate = screenshots_root / rel
        if candidate.is_file():
            cid = f"wm_{candidate.stem}"
            inline_images.append((str(candidate), cid))
            return (
                f'<img src="cid:{cid}" alt="{label}" width="100%"'
                f' style="display:block;max-width:560px;border-radius:6px;'
                f'border:1px solid #e2e8f0;" />',
                cid,
            )
        # File not on disk — fall back to a linked image (requires auth, best-effort)
        url = f"{site_url}/api/screenshots/{rel}"
        return (
            f'<a href="{url}" style="text-decoration:none;">'
            f'<img src="{url}" alt="{label}" width="100%"'
            f' style="display:block;max-width:560px;border-radius:6px;'
            f'border:1px solid #e2e8f0;" /></a>',
            '',
        )

    diff_img_tag, _      = _resolve(diff_rel,       'Visual diff')
    screenshot_img_tag, _ = _resolve(screenshot_rel, 'Full screenshot')

    def _img_section(img_tag: str, title: str, caption: str) -> str:
        if not img_tag:
            return ''
        return f'''
      <tr>
        <td style="padding:0 32px 8px;">
          <p style="margin:0 0 8px;font-size:11px;font-weight:700;color:#1d4ed8;
                    text-transform:uppercase;letter-spacing:0.8px;">{title}</p>
          {img_tag}
          <p style="margin:6px 0 0;font-size:12px;color:#9ca3af;text-align:center;">{caption}</p>
        </td>
      </tr>'''

    diff_section = _img_section(
        diff_img_tag,
        'Diff Highlight',
        'Brighter areas indicate more change between the last two screenshots',
    )
    screenshot_section = _img_section(
        screenshot_img_tag,
        'Full Screenshot',
        'The full page screenshot captured at the time of detection',
    )

    images_block = diff_section + ('\n      <tr><td style="padding:0 32px 16px;"></td></tr>\n' if diff_section and screenshot_section else '') + screenshot_section
    if images_block:
        images_block += '\n      <tr><td style="height:8px;"></td></tr>'

    # ------------------------------------------------------------------
    # HTML body — white & blue theme, table-based for email-client compat
    # ------------------------------------------------------------------
    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Visual change detected</title>
</head>
<body style="margin:0;padding:0;background:#eff6ff;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
         style="background:#eff6ff;padding:32px 0;">
    <tr>
      <td align="center">
        <!-- card -->
        <table width="600" cellpadding="0" cellspacing="0" role="presentation"
               style="background:#ffffff;border-radius:12px;overflow:hidden;
                      box-shadow:0 2px 12px rgba(37,99,235,0.10);max-width:600px;width:100%;">

          <!-- header bar -->
          <tr>
            <td style="background:#1d4ed8;padding:24px 32px;">
              <table width="100%" cellpadding="0" cellspacing="0" role="presentation">
                <tr>
                  <td>
                    <p style="margin:0;font-size:11px;color:#bfdbfe;text-transform:uppercase;
                               letter-spacing:1px;">Webpage Monitor</p>
                    <h1 style="margin:4px 0 0;font-size:22px;color:#ffffff;font-weight:700;">
                      Visual Change Detected
                    </h1>
                  </td>
                  <td align="right">
                    <span style="display:inline-block;background:#ffffff;color:{score_color};
                                 font-size:20px;font-weight:700;padding:8px 16px;
                                 border-radius:8px;letter-spacing:0.5px;">
                      {diff_score:.1f}%
                    </span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- URL row -->
          <tr>
            <td style="padding:20px 32px 0;">
              <p style="margin:0;font-size:11px;color:#6b7280;font-weight:600;
                         text-transform:uppercase;letter-spacing:0.8px;">Monitored page</p>
              <p style="margin:6px 0 0;font-size:15px;color:#1e3a8a;word-break:break-all;">
                <a href="{page.url}" style="color:#1d4ed8;text-decoration:none;font-weight:600;">{page.url}</a>
              </p>
            </td>
          </tr>

          <!-- stats row -->
          <tr>
            <td style="padding:16px 32px 20px;">
              <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
                     style="border:1px solid #dbeafe;border-radius:8px;overflow:hidden;background:#f0f7ff;">
                <tr>
                  <td width="50%" style="padding:14px 20px;border-right:1px solid #dbeafe;">
                    <p style="margin:0;font-size:11px;color:#6b7280;text-transform:uppercase;
                               letter-spacing:0.8px;">Change score</p>
                    <p style="margin:4px 0 0;font-size:28px;font-weight:700;color:{score_color};">
                      {diff_score:.1f}%
                    </p>
                    <p style="margin:2px 0 0;font-size:12px;color:#9ca3af;">
                      0% = identical &nbsp;·&nbsp; 100% = completely different
                    </p>
                  </td>
                  <td width="50%" style="padding:14px 20px;">
                    <p style="margin:0;font-size:11px;color:#6b7280;text-transform:uppercase;
                               letter-spacing:0.8px;">Detected at</p>
                    <p style="margin:4px 0 0;font-size:15px;font-weight:600;color:#111827;">
                      {checked_at_str}
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          {images_block}

          <!-- CTA button -->
          <tr>
            <td style="padding:4px 32px 32px;" align="center">
              <a href="{monitor_url}"
                 style="display:inline-block;background:#1d4ed8;color:#ffffff;
                        font-size:15px;font-weight:600;text-decoration:none;
                        padding:12px 32px;border-radius:8px;letter-spacing:0.3px;">
                Open Monitor Dashboard →
              </a>
            </td>
          </tr>

          <!-- footer -->
          <tr>
            <td style="background:#f8fafc;padding:16px 32px;border-top:1px solid #dbeafe;">
              <p style="margin:0;font-size:12px;color:#9ca3af;text-align:center;">
                You're receiving this because visual change notifications are enabled for this site.<br />
                To stop receiving these emails, disable <strong>Visual Change Notifications</strong>
                in the site settings.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    # ------------------------------------------------------------------
    # Plain-text fallback
    # ------------------------------------------------------------------
    text_body = "\n".join(filter(None, [
        "VISUAL CHANGE DETECTED",
        "",
        f"Page:         {page.url}",
        f"Change score: {diff_score:.1f}%  (0% = identical, 100% = completely different)",
        f"Detected at:  {checked_at_str}",
        f"Diff image:   {site_url}/api/screenshots/{diff_rel}" if diff_rel else "",
        f"Screenshot:   {screenshot_url}" if screenshot_url else "",
        "",
        f"Open monitor: {monitor_url}",
        "",
        "---",
        "To stop these emails, disable Visual Change Notifications in the site settings.",
    ]))

    _send_html(
        f"Visual change detected on {page.url} ({diff_score:.1f}%)",
        text_body,
        html_body,
        recipient,
        inline_images or None,
    )


def _consecutive_failures(page) -> int:
    """Return number of consecutive failed checks for the page (including the latest)."""
    count = 0
    checks = page.checks.order_by('-checked_at').values_list('is_up', flat=True)
    for is_up in checks:
        if is_up:
            break
        count += 1
    return count


def handle_post_check_notification(page, latest_check) -> None:
    """Send an email alert when the failure threshold is reached, and again when the site recovers."""
    if not getattr(page, 'notifications_enabled', False):
        return

    user = getattr(page, 'user', None)
    recipient = getattr(user, 'email', None)
    if not recipient:
        logger.warning(
            "Uptime notification skipped for page %s: user has no email address", page.url
        )
        return

    # --- Site recovered ---
    if latest_check.is_up:
        # Only send recovery email if the previous check was a failure
        prev = page.checks.order_by('-checked_at').exclude(pk=latest_check.pk).first()
        if prev and not prev.is_up:
            subject = f"Webpage RECOVERED: {page.url}"
            body = "\n".join([
                "Your monitored page is back online.",
                "",
                f"URL: {page.url}",
                f"Time: {latest_check.checked_at.isoformat()}",
                f"Status: {latest_check.status_code}",
            ])
            _send_plain(subject, body, recipient)
        return

    # --- Site is down ---
    threshold = int(getattr(page, 'alert_threshold', 0) or 0)
    if threshold <= 0:
        return

    failures = _consecutive_failures(page)

    # Only notify when we exactly hit the threshold to avoid repeated emails
    if failures != threshold:
        return

    subject = f"Webpage DOWN alert: {page.url}"
    status_display = str(latest_check.status_code) if latest_check.status_code is not None else 'ERR'
    body = "\n".join([
        "Your monitored page appears to be DOWN.",
        "",
        f"URL: {page.url}",
        f"Time: {latest_check.checked_at.isoformat()}",
        f"Status: {status_display}",
        f"Message: {latest_check.message or 'Unknown error'}",
        f"Consecutive failures: {failures} (threshold: {threshold})",
    ])
    _send_plain(subject, body, recipient)
