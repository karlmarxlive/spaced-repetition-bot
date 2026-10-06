"""Production HTTP, admin/contention, persistent restart; isolated /tmp only.
Run with a Python containing requirements.txt: python -m deploy.smoke
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def check_http(process, path='/admin/login/'):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'Supervisor exited: {process.returncode}\n' +
                               process.smoke_log.read_text()[-5000:])
        try:
            request = urllib.request.Request('http://127.0.0.1:8080' + path,
                headers={'Host': 'school.example.org', 'X-Forwarded-Proto': 'https'})
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            if error.code == 503:
                return error.code, error.read().decode()
            raise
        except (OSError, TimeoutError):
            time.sleep(.25)
    raise RuntimeError('HTTP startup timed out')


def data_scenario():
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from django.db import connections
    from django.test import Client
    from modules.delivery.application import daily_review
    from modules.materials.models import Course, Topic, Task
    from modules.users.models import Student
    from modules.users.services import assign_topics
    from modules.study_sessions.services import start_review, accept_answer
    from modules.repetitions.models import TopicProgress
    from modules.study_sessions.models import DailyReviewRun
    now = datetime(2026, 10, 6, 15, tzinfo=timezone.utc)
    user = get_user_model().objects.create_superuser('teacher', 'teacher@example.org', 'fixture-password-42')
    course = Course.objects.create(title='Before')
    topic = Topic.objects.create(course=course, title='Topic')
    Task.objects.create(topic=topic, title='Question', question='6*7?', answer='42', is_active=True)
    students = [Student.objects.create(course=course, telegram_id=i, first_name='Fixture') for i in (41, 42)]
    for student in students:
        assign_topics(student.pk, [topic.pk], now=now - timedelta(days=1))
    question = start_review(students[0].pk, now=now).question
    client = Client(HTTP_HOST='school.example.org', HTTP_X_FORWARDED_PROTO='https')
    client.force_login(user)
    connections.close_all()
    barrier = Barrier(3)
    def race(kind):
        try:
            barrier.wait(timeout=10)
            if kind == 'admin':
                response = client.post(f'/admin/materials/course/{course.pk}/change/',
                                       {'title': 'After', '_save': 'Save'})
                assert response.status_code == 302, response.status_code
            elif kind == 'answer':
                assert accept_answer(students[0].pk, question.attempt_id, '42', now=now).status == 'correct'
            else:
                daily_review(students[1].pk, now=now, bot_id=12345)
        finally:
            connections.close_all()
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(race, ['admin', 'answer', 'scheduler']))
    course.refresh_from_db()
    assert course.title == 'After'
    assert TopicProgress.objects.get(assignment__student=students[0]).interval_step == 1
    assert DailyReviewRun.objects.filter(student=students[1]).count() == 1
    print('Production admin + answer + daily review concurrency: OK')


def main():
    # Reserve/check the production port before creating data or starting processes.
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 8080))
    with TemporaryDirectory(prefix='stage8-smoke-') as directory:
        root = Path(directory)
        env = {**os.environ, 'DJANGO_SETTINGS_MODULE': 'config.production', 'DJANGO_DEBUG': 'false',
               'DJANGO_SECRET_KEY': 'isolated-smoke-key-' * 5,
               'DJANGO_ALLOWED_HOSTS': 'school.example.org',
               'DJANGO_CSRF_TRUSTED_ORIGINS': 'https://school.example.org',
               'DJANGO_TRUST_PROXY_HTTPS': 'true', 'DJANGO_DB_PATH': str(root / 'db.sqlite3'),
               'DJANGO_MEDIA_ROOT': str(root / 'media'), 'RUNTIME_DIR': str(root / 'runtime'),
               'TELEGRAM_BOT_TOKEN': '', 'TELEGRAM_BOT_USERNAME': '', 'APP_MODE': 'setup',
               'BACKUP_EXPORT_COMMAND': ''}
        def start():
            with (root / 'process.log').open('a') as output:
                process = subprocess.Popen([sys.executable, '-m', 'deploy.supervisor'], cwd=ROOT, env=env,
                                           stdout=output, stderr=output)
                process.smoke_log = root / 'process.log'
                return process
        def stop(process):
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=30)
        process = start()
        try:
            status, page = check_http(process)
            assert status == 200
            assets = re.findall(r'(?:href|src)="(/static/[^\"]+\.(?:css|js))"', page)
            assert any(x.endswith('.css') for x in assets)
            assert any(x.endswith('.js') for x in assets)
            for asset in assets:
                assert check_http(process, asset)[0] == 200
            assert check_http(process, '/health/ready/')[0] == 503  # No teacher yet.
            # A second supervisor on this volume must fail before migrations/polling.
            duplicate = start()
            assert duplicate.wait(timeout=120) == 1
            result = subprocess.run([sys.executable, '-m', 'deploy.smoke', '--data'],
                                    cwd=ROOT, env=env, timeout=120, check=True)
            assert check_http(process, '/health/ready/')[0] == 200
            (root / 'media/fixture.txt').write_text('persisted')
        finally:
            stop(process)
        process = start()
        try:
            assert check_http(process)[0] == 200
            assert check_http(process, '/health/ready/')[0] == 200
            import sqlite3
            with sqlite3.connect(root / 'db.sqlite3') as db:
                assert db.execute('SELECT title FROM materials_course').fetchone()[0] == 'After'
            assert (root / 'media/fixture.txt').read_text() == 'persisted'
            assert list((root / 'backups').glob('*.tar.gz'))
        finally:
            stop(process)
        # Failure of migrations must prevent even the admin listener.
        env['DJANGO_DB_PATH'] = str(root / 'corrupt.sqlite3')
        (root / 'corrupt.sqlite3').write_text('not sqlite')
        broken = start()
        assert broken.wait(timeout=120) == 1
    print('Production HTTP/static, lock, readiness, migrations and persistent restart: OK')


if __name__ == '__main__':
    if '--data' in sys.argv:
        data_scenario()
    else:
        main()
