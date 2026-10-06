FROM python:3.14.6-slim-bookworm@sha256:4c92ffcde4dd6f1ff72a24518f49fd4990b27134987dfa31a733badde66df9f8
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=config.production DJANGO_DEBUG=false \
    DJANGO_DB_PATH=/data/db.sqlite3 DJANGO_MEDIA_ROOT=/data/media
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt && python -m pip check
COPY . .
# Static collection uses isolated build settings and never opens /data.
RUN DJANGO_SETTINGS_MODULE=config.build python manage.py collectstatic --noinput
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s CMD ["python", "-m", "deploy.probe"]
CMD ["python", "-m", "deploy.supervisor"]
