"""Console logging with secrets and Telegram URLs redacted, including tracebacks."""

import logging
import os
import re
import traceback


def log_failure(logger, message, error):
    """Keep failure type and code locations, without exception text or local data."""
    logger.error("%s (%s)\n%s", message, type(error).__name__,
                 "".join(f"  {frame.filename}:{frame.lineno} in {frame.name}\n"
                         for frame in traceback.extract_tb(error.__traceback__)))


class SafeFormatter(logging.Formatter):
    def format(self, record):
        output = super().format(record)
        output = re.sub(r"https?://[^\s]*api\.telegram\.org[^\s]*", "[TELEGRAM_URL]", output)
        for name in ("TELEGRAM_BOT_TOKEN", "DJANGO_SECRET_KEY"):
            secret = os.environ.get(name)
            if secret:
                output = output.replace(secret, "[REDACTED]")
        return re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[TOKEN]", output)


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"safe": {"()": SafeFormatter, "format": "{asctime} {levelname} {name}: {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "safe"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
