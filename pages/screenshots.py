"""Screenshot capture and visual-diff utilities.

Uses Playwright (headless Chromium) to capture full-page screenshots
and Pillow + imagehash for perceptual diff scoring.

Storage backend (local disk or S3) is accessed through
``pages.storage.screenshot_storage``.  No code in this module touches
the filesystem directly — all I/O goes through the storage abstraction.
"""

import hashlib
import io
import logging
import os
import tempfile
from pathlib import Path

from django.conf import settings
from PIL import Image

from .storage import screenshot_storage as storage

logger = logging.getLogger(__name__)

# Maximum number of screenshots to keep per monitored page.
MAX_SCREENSHOTS_PER_PAGE = 30

# JPEG quality for all saved images (1-95).
JPEG_QUALITY = int(getattr(settings, "SCREENSHOT_JPEG_QUALITY", 82))

# Thumbnail width in pixels.
THUMBNAIL_WIDTH = 480
THUMBNAIL_JPEG_QUALITY = 55


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _screenshots_root() -> Path:
    """Return the local screenshots root (local storage only).

    Used by legacy code paths that still need an absolute path.
    For S3 storage, prefer the storage abstraction directly.
    """
    root = Path(getattr(settings, "SCREENSHOTS_DIR",
                        os.path.join(settings.BASE_DIR, "screenshots")))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _unique_filename(page_id: int, suffix: str = ".jpg") -> str:
    """Return a relative path like ``<page_id>/<hash><suffix>``."""
    import time
    ts = str(time.time_ns())
    h = hashlib.md5(ts.encode()).hexdigest()[:10]
    return os.path.join(str(page_id), f"{h}{suffix}")


def _img_to_jpeg_bytes(img: Image.Image, quality: int = JPEG_QUALITY) -> bytes:
    """Convert a Pillow image to JPEG bytes."""
    if img.mode in ("RGBA", "P", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _save_jpeg(img: Image.Image, path) -> None:
    """Save a Pillow image as JPEG to a local path (used during capture only)."""
    data = _img_to_jpeg_bytes(img)
    Path(path).write_bytes(data)


def _open_image_from_storage(rel_path: str) -> Image.Image:
    """Load a Pillow Image from storage (local or S3)."""
    local = storage.local_path(rel_path)
    _tmp_created = False
    if local is None:
        # S3: local_path() already downloaded it to a temp file
        raise FileNotFoundError(f"Could not obtain local path for {rel_path}")
    try:
        img = Image.open(str(local))
        img.load()   # force decode before the file handle may close
        return img
    finally:
        # If the path was a temp file created by S3 backend, clean it up
        if _tmp_created and local.exists():
            local.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Thumbnail helpers
# ---------------------------------------------------------------------------

def create_thumbnail(source_rel: str, page_id: int) -> str:
    """Generate a low-res thumbnail from an existing screenshot.

    Saves it as ``<stem>_thumb.jpg`` and returns its rel-path, or '' on failure.
    """
    thumb_rel = _thumb_rel(source_rel)
    if storage.exists(thumb_rel):
        return thumb_rel
    try:
        local = storage.local_path(source_rel)
        if local is None:
            return ''
        with Image.open(str(local)) as img:
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            w, h = img.size
            new_h = max(1, int(h * THUMBNAIL_WIDTH / w))
            thumb = img.resize((THUMBNAIL_WIDTH, new_h), Image.LANCZOS)
            data = _img_to_jpeg_bytes(thumb, quality=THUMBNAIL_JPEG_QUALITY)
        storage.save(thumb_rel, data)
        logger.debug("Thumbnail saved: %s", thumb_rel)
        return thumb_rel
    except Exception:
        logger.warning("Failed to create thumbnail for %s", source_rel, exc_info=True)
        return ''


def _thumb_rel(source_rel: str) -> str:
    p = Path(source_rel)
    return str(p.with_name(p.stem + "_thumb.jpg"))


def get_or_create_thumbnail(source_rel: str, page_id: int) -> str:
    if not source_rel:
        return ''
    thumb_rel = _thumb_rel(source_rel)
    if storage.exists(thumb_rel):
        return thumb_rel
    return create_thumbnail(source_rel, page_id)


# ---------------------------------------------------------------------------
# Delete helpers
# ---------------------------------------------------------------------------

def delete_screenshot_file(rel_path: str) -> None:
    """Delete an artefact from storage (local or S3)."""
    if rel_path:
        storage.delete(rel_path)


# ---------------------------------------------------------------------------
# Region crop
# ---------------------------------------------------------------------------

def _crop_to_region(full_rel: str, page_id: int) -> str | None:
    """Create a cropped copy of a screenshot for diff comparison.

    Returns the rel-path of the crop, or None if not needed.
    """
    from .models import MonitoredPage

    try:
        page = MonitoredPage.objects.get(id=page_id)
    except MonitoredPage.DoesNotExist:
        return None

    l, t, w, h = (page.region_left_pct, page.region_top_pct,
                  page.region_width_pct, page.region_height_pct)

    if w >= 1.0 and h >= 1.0 and l <= 0 and t <= 0:
        return None

    try:
        local = storage.local_path(full_rel)
        if local is None:
            return None
        with Image.open(str(local)) as img:
            box = (int(l * img.width), int(t * img.height),
                   int((l + w) * img.width), int((t + h) * img.height))
            cropped = img.crop(box)
            data = _img_to_jpeg_bytes(cropped)
        p = Path(full_rel)
        crop_rel = str(p.with_name(p.stem + "_crop.jpg"))
        storage.save(crop_rel, data)
        logger.debug("Crop saved: %s", crop_rel)
        return crop_rel
    except Exception:
        logger.warning("Failed to crop screenshot for page %s", page_id, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Screenshot capture (Playwright)
# ---------------------------------------------------------------------------

def capture_screenshot(url: str, page_id: int, timeout_ms: int = 30_000) -> tuple[str, str]:
    """Capture a full-page screenshot and store it via the storage backend.

    Returns ``(full_rel_path, crop_rel_path)``.  Both are empty strings on failure.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("playwright is not installed – skipping screenshot")
        return ("", "")

    rel_path = _unique_filename(page_id, ".jpg")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            # Playwright only captures PNG natively
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            page.screenshot(path=str(tmp_path), full_page=True)
            context.close()
            browser.close()

        # Convert PNG → JPEG
        with Image.open(str(tmp_path)) as raw:
            jpeg_data = _img_to_jpeg_bytes(raw)
        tmp_path.unlink(missing_ok=True)

        # Upload to storage
        storage.save(rel_path, jpeg_data)
        logger.info("Screenshot saved: %s", rel_path)

        # Region crop (for diff)
        crop_rel = _crop_to_region(rel_path, page_id) or ""

        # Pre-generate thumbnail
        create_thumbnail(rel_path, page_id)

        return (rel_path, crop_rel)

    except Exception:
        logger.exception("Failed to capture screenshot for page %s (%s)", page_id, url)
        return ("", "")


# ---------------------------------------------------------------------------
# Visual diff
# ---------------------------------------------------------------------------

def compute_diff(prev_rel_path: str, curr_rel_path: str, page_id: int) -> tuple[str, float | None]:
    """Compare two screenshots and produce a blue-overlay highlighted diff.

    Returns ``(diff_rel_path, diff_score)`` where score is 0-100.
    """
    if not prev_rel_path or not curr_rel_path:
        return ("", None)

    if not storage.exists(prev_rel_path) or not storage.exists(curr_rel_path):
        return ("", None)

    try:
        import imagehash
        from PIL import ImageChops
    except ImportError:
        logger.error("Pillow / imagehash not installed – skipping diff")
        return ("", None)

    # Download both images to local temp files for Pillow processing
    prev_local = storage.local_path(prev_rel_path)
    curr_local = storage.local_path(curr_rel_path)
    _prev_is_tmp = prev_local and not prev_local.exists()  # S3 temp
    _curr_is_tmp = curr_local and not curr_local.exists()  # S3 temp

    if prev_local is None or curr_local is None:
        return ("", None)

    try:
        img_prev = Image.open(str(prev_local)).convert("RGB")
        img_curr = Image.open(str(curr_local)).convert("RGB")

        w = max(img_prev.width, img_curr.width)
        h = max(img_prev.height, img_curr.height)
        canvas_prev = Image.new("RGB", (w, h), (255, 255, 255))
        canvas_curr = Image.new("RGB", (w, h), (255, 255, 255))
        canvas_prev.paste(img_prev, (0, 0))
        canvas_curr.paste(img_curr, (0, 0))

        hash_prev = imagehash.phash(canvas_prev)
        hash_curr = imagehash.phash(canvas_curr)
        score = round(((hash_prev - hash_curr) / 64) * 100, 2)

        diff_rel = ""
        if score > 0:
            raw_diff = ImageChops.difference(canvas_prev, canvas_curr)
            THRESHOLD, ALPHA = 10, 0.55
            H_R, H_G, H_B = 59, 130, 246  # blue #3b82f6

            r_diff, g_diff, b_diff = raw_diff.split()
            r_curr, g_curr, b_curr = canvas_curr.split()

            def _thresh(ch):
                return ch.point(lambda v: 255 if v > THRESHOLD else 0)

            changed = ImageChops.lighter(
                ImageChops.lighter(_thresh(r_diff), _thresh(g_diff)), _thresh(b_diff)
            )

            def _blend(curr_ch, hv):
                blended = curr_ch.point(lambda v: int(v * (1 - ALPHA) + hv * ALPHA))
                out = Image.new("L", curr_ch.size)
                out.paste(blended, mask=changed)
                out.paste(curr_ch, mask=ImageChops.invert(changed))
                return out

            diff_img = Image.merge("RGB", (
                _blend(r_curr, H_R), _blend(g_curr, H_G), _blend(b_curr, H_B)
            ))
            diff_data = _img_to_jpeg_bytes(diff_img)
            diff_rel = _unique_filename(page_id, "_diff.jpg")
            storage.save(diff_rel, diff_data)
            logger.info("Blue-overlay diff saved: %s", diff_rel)

        logger.info("Diff score for page %s: %.2f%%", page_id, score)
        return (diff_rel, score)

    except Exception:
        logger.exception("Failed to compute diff for page %s", page_id)
        return ("", None)


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def cleanup_old_screenshots(page) -> None:
    """Delete artefacts exceeding the retention limit."""
    from .models import MonitoredPageCheck

    limit = getattr(settings, "MAX_SCREENSHOTS_PER_PAGE", MAX_SCREENSHOTS_PER_PAGE)
    checks_with_ss = (
        MonitoredPageCheck.objects
        .filter(page=page)
        .exclude(screenshot_path="")
        .order_by("-checked_at")
    )
    to_prune = list(checks_with_ss[limit:])
    if not to_prune:
        return

    for check in to_prune:
        delete_screenshot_file(check.screenshot_path)
        delete_screenshot_file(check.crop_path)
        delete_screenshot_file(check.diff_path)
        # Also remove the thumbnail
        if check.screenshot_path:
            delete_screenshot_file(_thumb_rel(check.screenshot_path))
        check.screenshot_path = ""
        check.crop_path = ""
        check.diff_path = ""
        check.save(update_fields=["screenshot_path", "crop_path", "diff_path"])
