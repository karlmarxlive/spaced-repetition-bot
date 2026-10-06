"""One owner per persistent SQLite volume; fail as a unit, bounded shutdown."""
import argparse
import fcntl
import logging
import logging.config
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from config.logging import LOGGING, log_failure
from deploy.backup import create_backup, restore
from deploy.health import directory, mark
from deploy.bootstrap import create_teacher, take_credentials

logger = logging.getLogger(__name__)
STOP_TIMEOUT = 25
BACKUP_INTERVAL = 86400


def stop_children(children):
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + STOP_TIMEOUT
    for child in children:
        try:
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    # Kill surviving grandchildren too, even if their group leader exited.
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for child in children:
        child.wait()
    children.clear()
    # As container PID 1 we adopt grandchildren when a component crashes.
    # Reap them after the owned process groups have been killed.
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            time.sleep(0.02)


def supervise(mode, children, stopping, backup):
    commands = [[sys.executable, '-m', 'gunicorn', 'config.wsgi:application',
                 '-c', 'deploy/gunicorn.conf.py']]
    if mode == 'run':
        commands += [[sys.executable, '-m', 'bot'], [sys.executable, 'manage.py', 'run_scheduler']]
    next_backup = time.monotonic() + BACKUP_INTERVAL
    next_monitor = time.monotonic() + 60
    while not stopping[0]:
        if not children:
            for command in commands:
                if stopping[0]:
                    return 0
                children.append(subprocess.Popen(command, start_new_session=True))
        mark('supervisor')
        for child in children:
            if child.poll() is not None:
                logger.error('Компонент завершился с кодом %s', child.returncode)
                return 1
        if time.monotonic() >= next_monitor:
            from deploy.health import ready
            issues = ready()
            if issues:
                logger.warning('Проверка готовности: %s', ', '.join(issues))
            next_monitor = time.monotonic() + 60
        if mode == 'run' and time.monotonic() >= next_backup:
            # Pause every writer including admin/uploads before snapshotting.
            stop_children(children)
            backup()
            next_backup = time.monotonic() + BACKUP_INTERVAL
        time.sleep(0.5)
    return 0


def main(argv=None):
    credentials = take_credentials()
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['setup', 'run', 'backup', 'restore'], nargs='?',
                        default=os.environ.get('APP_MODE', 'setup'))
    parser.add_argument('--archive')
    args = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.production')
    os.environ['APP_MANAGED'] = '1'
    logging.config.dictConfig(LOGGING)
    children, stopping, lock = [], [False], None
    def shutdown(signum, frame):
        stopping[0] = True
        # Also interrupt migrations/exports. No new working services after signal.
        for child in children:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        raise InterruptedError("Stopping")
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        import django
        django.setup()
        from django.conf import settings
        from django.contrib.auth import get_user_model
        db = Path(settings.DATABASES['default']['NAME'])
        media = Path(settings.MEDIA_ROOT)
        db.parent.mkdir(parents=True, exist_ok=True)
        lock = (db.parent / '.runtime.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def backup():
            path = create_backup(db, media, os.environ.get('BACKUP_DIR', str(db.parent / 'backups')),
                                 export=args.mode != 'setup' or bool(os.environ.get('BACKUP_EXPORT_COMMAND')))
            mark('backup')
            logger.info('Резервная копия создана: %s', path.name)
        if args.mode == 'restore':
            if not args.archive:
                raise ValueError('--archive required')
            restore(args.archive, db, media)
            return 0
        media.mkdir(parents=True, exist_ok=True)
        directory().mkdir(parents=True, exist_ok=True)
        # Runtime state belongs to this lifecycle, never reuse stale failures.
        for file in directory().iterdir():
            if file.is_file():
                file.unlink()
        (directory() / 'mode').write_text(args.mode)
        if args.mode == 'backup':
            backup()
            return 0
        if args.mode == 'run' and not os.environ.get('BACKUP_EXPORT_COMMAND'):
            raise ValueError('Run requires off-application BACKUP_EXPORT_COMMAND')
        if db.exists() and db.stat().st_size:
            backup()  # Before any schema changes, while holding volume lock.
        # No migration or data access during image build.
        def checked(command):
            if stopping[0]:
                raise InterruptedError('Stopping')
            child = subprocess.Popen([sys.executable, 'manage.py', *command], start_new_session=True)
            children.append(child)
            code = child.wait()
            children.remove(child)
            if code or stopping[0]:
                raise RuntimeError('Startup command failed')
        checked(['migrate', '--noinput'])
        checked(['check', '--deploy', '--fail-level', 'WARNING'])
        if args.mode == 'setup' and create_teacher(credentials):
            logger.info('Первый учитель создан; удалите одноразовый секрет из окружения платформы')
        credentials = None
        if args.mode == 'run':
            if not get_user_model().objects.filter(is_superuser=True, is_active=True).exists():
                raise RuntimeError('Create teacher in setup mode first')
            if not os.environ.get('TELEGRAM_BOT_TOKEN'):
                raise ValueError('TELEGRAM_BOT_TOKEN required')
            backup()
        return supervise(args.mode, children, stopping, backup)
    except InterruptedError:
        return 0 if stopping[0] else 1
    except Exception as error:
        log_failure(logger, 'Контейнер остановлен', error)
        return 1
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        stop_children(children)
        if lock is not None:
            lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
