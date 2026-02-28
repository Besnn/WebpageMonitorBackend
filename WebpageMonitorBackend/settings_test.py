"""Test settings.

Use SQLite for unit tests so the suite can run without Postgres.

Run:
  py backend\manage.py test --settings=WebpageMonitorBackend.settings_test
"""

from .settings import *  # noqa

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

