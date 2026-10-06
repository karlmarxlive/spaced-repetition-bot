"""Offline configuration selected before Django setup; never uses local data."""
import os
from tempfile import TemporaryDirectory

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_BOT_USERNAME": "", "DJANGO_SECRET_KEY": "offline-test-secret",
    "DJANGO_DEBUG": "false", "DJANGO_ALLOWED_HOSTS": "testserver,localhost,127.0.0.1",
})

from config.settings import *  # noqa: E402,F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:",
                         "OPTIONS": {"transaction_mode": "IMMEDIATE"}}}
_media_directory = TemporaryDirectory(prefix="materials-tests-")
MEDIA_ROOT = _media_directory.name
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
