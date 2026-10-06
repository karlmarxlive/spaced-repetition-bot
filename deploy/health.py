"""Ephemeral activity markers, no tokens or event payloads."""
import os
from pathlib import Path
import time


def directory():
    return Path(os.environ.get('RUNTIME_DIR', '/tmp/study-bot-runtime'))


def mark(name):
    # Local development and tests do not write production activity markers.
    if os.environ.get('APP_MANAGED') != '1':
        return
    path = directory() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def fail_once(name):
    if not (directory() / name).exists():
        mark(name)


def clear(name):
    if os.environ.get("APP_MANAGED") == "1":
        (directory() / name).unlink(missing_ok=True)


def age(name):
    try:
        return time.time() - (directory() / name).stat().st_mtime
    except FileNotFoundError:
        return float('inf')


def ready():
    from django.db import connection
    from django.utils import timezone
    from modules.delivery.models import OutgoingMessage
    from datetime import timedelta
    from django.contrib.auth import get_user_model
    issues = []
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
    if age('supervisor') > 15:
        issues.append('supervisor')
    if not get_user_model().objects.filter(is_superuser=True, is_active=True).exists():
        issues.append('teacher')
    mode = (directory() / 'mode').read_text().strip()
    if mode == 'run':
        for name, limit in [('scheduler', 120), ('polling', 120), ('backup', 90000)]:
            if age(name) > limit:
                issues.append(name)
        failure = directory() / 'scheduler-error'
        if failure.exists() and age('scheduler-error') > 300:
            issues.append('scheduler_error')
        errors = list(directory().glob('incoming-error-*'))
        if any(time.time() - p.stat().st_mtime > 300 for p in errors):
            issues.append('incoming')
        pending = OutgoingMessage.objects.filter(state__in=['pending', 'retry', 'sending'])
        if pending.count() > 100 or pending.filter(
            created_at__lt=timezone.now() - timedelta(minutes=15)).exists():
            issues.append('outbox')
        if OutgoingMessage.objects.filter(state='failed').exists():
            issues.append('outbox_failed')
    return issues


def readiness(request):
    from django.http import JsonResponse
    try:
        issues = ready()
    except Exception:
        issues = ['state_unavailable']
    return JsonResponse({'ready': not issues, 'issues': issues}, status=503 if issues else 200)


def liveness(request):
    from django.http import JsonResponse
    live = age('supervisor') < 15
    return JsonResponse({'live': live}, status=200 if live else 503)
