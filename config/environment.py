import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def load_environment(path=BASE_DIR / ".env"):
    if os.environ.get("DJANGO_SETTINGS_MODULE") in {"config.production", "config.build"}:
        return
    # Interpolation disabled: values are literal, including dollar signs in secrets.
    load_dotenv(path, override=False, interpolate=False)


def env_bool(name, default=False):
    value = os.environ.get(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"{name}: ожидается true или false")
    return value in {"true", "1", "yes"}
