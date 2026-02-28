import logging
import os
import sys
import threading
from django.apps import AppConfig
from django.core.management import call_command

logger = logging.getLogger(__name__)


class PagesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pages"

    _run_checks_started = False

    def ready(self):
        # Skip during management commands that are not the server itself
        # (e.g. migrate, makemigrations, shell, collectstatic …)
        _non_server_commands = {
            "migrate", "makemigrations", "shell", "collectstatic",
            "createsuperuser", "test", "check", "inspectdb",
            "run_checks",  # avoid recursive start if run manually
        }
        argv0 = sys.argv[0] if sys.argv else ""
        cmd = sys.argv[1] if len(sys.argv) > 1 else ""

        # Detect Django dev server reloader child (the actual worker)
        is_dev_server = (
            cmd == "runserver"
            and (os.environ.get("RUN_MAIN") == "true" or "--noreload" in sys.argv)
        )

        # Detect production WSGI/ASGI servers: gunicorn, uvicorn, daphne
        is_prod_server = any(
            prog in argv0
            for prog in ("gunicorn", "uvicorn", "daphne")
        )

        # Also handle: python -m uvicorn / python -m gunicorn
        is_module_server = (
            len(sys.argv) > 0
            and sys.argv[0] == "-m"
            or (len(sys.argv) > 1 and sys.argv[1] in ("uvicorn", "gunicorn", "daphne"))
        )

        should_start = (is_dev_server or is_prod_server or is_module_server) and cmd not in _non_server_commands

        if not should_start or PagesConfig._run_checks_started:
            return

        PagesConfig._run_checks_started = True
        logger.info("Starting background monitor checker thread")
        thread = threading.Thread(target=self._start_run_checks, daemon=True, name="run_checks")
        thread.start()

    def _start_run_checks(self):
        try:
            call_command("run_checks", "--interval", "60")
        except Exception:  # pragma: no cover
            logger.exception("Background monitor checks failed")
