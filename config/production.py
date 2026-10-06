"""Fail closed server configuration; selected explicitly by the container."""
from pathlib import Path
from config.settings import *  # noqa: F403

if DEBUG:
    raise ImproperlyConfigured("Сервер требует DJANGO_DEBUG=false")
if not ALLOWED_HOSTS or not os.environ.get("DJANGO_ALLOWED_HOSTS") or any(
    host == "*" or host.startswith(".") for host in ALLOWED_HOSTS
):
    raise ImproperlyConfigured("Задайте конкретные DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = [item.strip() for item in os.environ.get(
    "DJANGO_CSRF_TRUSTED_ORIGINS", ""
).split(",") if item.strip()]
if not CSRF_TRUSTED_ORIGINS or any(
    not item.startswith("https://") or "*" in item for item in CSRF_TRUSTED_ORIGINS
):
    raise ImproperlyConfigured("Задайте конкретные HTTPS DJANGO_CSRF_TRUSTED_ORIGINS")
DATABASES["default"]["NAME"] = os.environ.get("DJANGO_DB_PATH", "/data/db.sqlite3")
MEDIA_ROOT = os.environ.get("DJANGO_MEDIA_ROOT", "/data/media")
for value in (DATABASES["default"]["NAME"], MEDIA_ROOT):
    if not Path(value).is_absolute():
        raise ImproperlyConfigured("Серверные пути базы и media должны быть абсолютными")
if Path(DATABASES["default"]["NAME"]).resolve().is_relative_to(Path(MEDIA_ROOT).resolve()):
    raise ImproperlyConfigured("База должна находиться вне media")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r"^health/ready/$", r"^health/live/$"]
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
# Enable only after confirming the ingress overwrites this header.
if env_bool("DJANGO_TRUST_PROXY_HTTPS", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
