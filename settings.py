"""Compatibility settings module for test runners.

This module exists to satisfy environments that invoke Django with
--settings=settings (e.g., some IDE defaults). It re-exports our
project settings, preferring the lightweight SQLite-based test
configuration when available.
"""

try:
    # Prefer the test settings to ensure the suite runs without Postgres
    from WebpageMonitorBackend.settings_test import *  # noqa: F401,F403
except Exception:  # pragma: no cover - fallback for non-test contexts
    from WebpageMonitorBackend.settings import *  # noqa: F401,F403
