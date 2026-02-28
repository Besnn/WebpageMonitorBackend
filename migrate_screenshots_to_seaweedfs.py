"""
migrate_screenshots_to_seaweedfs.py
------------------------------------
One-shot script that uploads every file under backend/screenshots/ to SeaweedFS,
preserving the relative path structure (e.g. 12/abc123.jpg).

Run from the backend/ directory with the venv activated:

    python migrate_screenshots_to_seaweedfs.py

The script is idempotent — it skips files that already exist in SeaweedFS.
"""

import os
import sys
import django
from pathlib import Path

# Bootstrap Django so we can use settings + storage layer
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "WebpageMonitorBackend.settings")
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
django.setup()

from django.conf import settings
from pages.storage import screenshot_storage as storage

LOCAL_ROOT = Path(getattr(settings, "SCREENSHOTS_DIR", BASE_DIR / "screenshots"))

def main():
    if not LOCAL_ROOT.exists():
        print(f"No local screenshots directory found at {LOCAL_ROOT}. Nothing to migrate.")
        return

    files = list(LOCAL_ROOT.rglob("*"))
    image_files = [f for f in files if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png")]

    if not image_files:
        print("No image files found to migrate.")
        return

    print(f"Found {len(image_files)} image(s) under {LOCAL_ROOT}")
    print(f"Target: {settings.SEAWEEDFS_ENDPOINT} / bucket={settings.SEAWEEDFS_BUCKET}\n")

    uploaded = 0
    skipped  = 0
    failed   = 0

    for abs_path in sorted(image_files):
        rel = abs_path.relative_to(LOCAL_ROOT).as_posix()  # e.g. "12/abc123.jpg"

        if storage.exists(rel):
            print(f"  [skip]   {rel}")
            skipped += 1
            continue

        try:
            data = abs_path.read_bytes()
            storage.save(rel, data)
            print(f"  [upload] {rel}  ({len(data):,} bytes)")
            uploaded += 1
        except Exception as exc:
            print(f"  [ERROR]  {rel}: {exc}")
            failed += 1

    print(f"\nDone — uploaded: {uploaded}, skipped: {skipped}, failed: {failed}")

if __name__ == "__main__":
    main()

