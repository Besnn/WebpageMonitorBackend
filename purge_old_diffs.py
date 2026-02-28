"""One-off script: delete all old *_diff.png files from screenshots dir.

Old diff images were generated with ImageChops.difference() which produces
a near-black (inverted) image.  The new code generates blue-overlay diffs.

Run once after updating screenshots.py and before restarting the server:
    python manage.py shell < purge_old_diffs.py
Or:
    python purge_old_diffs.py
"""
import os
import sys
from pathlib import Path

# Find the screenshots root
SCRIPT_DIR = Path(__file__).resolve().parent
SCREENSHOTS_DIR = SCRIPT_DIR / "screenshots"

if not SCREENSHOTS_DIR.is_dir():
    print(f"Screenshots dir not found at {SCREENSHOTS_DIR}")
    sys.exit(1)

count = 0
for diff_file in SCREENSHOTS_DIR.rglob("*_diff.png"):
    print(f"  Deleting {diff_file}")
    diff_file.unlink()
    count += 1

print(f"\nDeleted {count} old diff file(s).")

# Also clear the diff_path field in the DB for all checks that reference deleted files.
# This part only works when run via `python manage.py shell`.
try:
    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "WebpageMonitorBackend.settings")
    django.setup()

    from pages.models import MonitoredPageCheck
    updated = MonitoredPageCheck.objects.exclude(diff_path="").update(diff_path="")
    print(f"Cleared diff_path on {updated} check record(s) in the DB.")
except Exception as exc:
    print(f"(Skipped DB cleanup: {exc})")
    print("To also clear DB references, run: python manage.py shell < purge_old_diffs.py")

