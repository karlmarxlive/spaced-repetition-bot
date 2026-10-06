"""Static assets only: no runtime secrets, database connections or migrations."""
import os
os.environ['DJANGO_DEBUG'] = 'true'
os.environ['DJANGO_SECRET_KEY'] = 'static-build-only-never-runtime'
from config.settings import *  # noqa: E402,F403
DEBUG = False
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}
