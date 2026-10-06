"""Data recovery and process lifecycle checks without Telegram or local data."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from deploy.backup import create_backup, restore
from deploy.supervisor import stop_children, supervise


class BackupTests(unittest.TestCase):
    def test_snapshot_connections_are_closed_before_temporary_cleanup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / 'media'
            media.mkdir()
            db = root / 'db.sqlite3'
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE lesson (id INTEGER)')
            connection.close()
            handles = []
            connect = sqlite3.connect

            def tracked_connect(*args, **kwargs):
                handle = connect(*args, **kwargs)
                handles.append(handle)
                return handle

            with patch('deploy.backup.sqlite3.connect', side_effect=tracked_connect):
                archive = create_backup(db, media, root / 'backups', export=False)
                restore(archive, root / 'restored/db.sqlite3', root / 'restored/media')
            self.assertEqual(len(handles), 3)
            for handle in handles:
                with self.assertRaises(sqlite3.ProgrammingError):
                    handle.execute('SELECT 1')

    def test_snapshot_restore_checksums_retention_and_external_copy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db, media = root / 'db.sqlite3', root / 'media'
            media.mkdir()
            (media / 'lesson.txt').write_text('lesson')
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE lesson (answer TEXT)')
                connection.execute("INSERT INTO lesson VALUES ('42')")
            exporter = [sys.executable, '-c',
                        'import shutil,sys; shutil.copyfile(sys.argv[2],sys.argv[1])',
                        str(root / 'external.tar.gz')]
            with patch.dict(os.environ, {'BACKUP_KEEP': '1', 'BACKUP_EXPORT_COMMAND': json.dumps(exporter)}):
                create_backup(db, media, root / 'backups')
                archive = create_backup(db, media, root / 'backups')
            self.assertEqual(len(list((root / 'backups').glob('*.tar.gz'))), 1)
            self.assertEqual(archive.read_bytes(), (root / 'external.tar.gz').read_bytes())
            restored = root / 'restored'
            restore(archive, restored / 'db.sqlite3', restored / 'media')
            with sqlite3.connect(restored / 'db.sqlite3') as connection:
                self.assertEqual(connection.execute('SELECT answer FROM lesson').fetchone()[0], '42')
            self.assertEqual((restored / 'media/lesson.txt').read_text(), 'lesson')
            with self.assertRaises(RuntimeError):
                restore(archive, restored / 'db.sqlite3', restored / 'media')
            unpacked = root / 'tampered'
            with tarfile.open(archive) as source:
                source.extractall(unpacked, filter='data')
            (unpacked / 'media/lesson.txt').write_text('tampered')
            bad = root / 'bad.tar.gz'
            with tarfile.open(bad, 'w:gz') as output:
                for item in unpacked.iterdir():
                    output.add(item, arcname=item.name)
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                restore(bad, root / 'bad/db.sqlite3', root / 'bad/media')
            self.assertFalse((root / 'bad/db.sqlite3').exists())

    def test_failed_export_retains_recovery_archive(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / 'media'
            media.mkdir()
            with sqlite3.connect(root / 'db.sqlite3') as connection:
                connection.execute('CREATE TABLE item (id INTEGER)')
            with patch.dict(os.environ, {'BACKUP_EXPORT_COMMAND': json.dumps([sys.executable, '-c', 'exit(7)'])}):
                with self.assertRaises(subprocess.CalledProcessError):
                    create_backup(root / 'db.sqlite3', media, root / 'backups')
            self.assertEqual(len(list((root / 'backups').glob('*.tar.gz'))), 1)


class SupervisorTests(unittest.TestCase):
    def test_component_failure_stops_start_cycle(self):
        children = []
        popen = subprocess.Popen
        with patch('deploy.supervisor.subprocess.Popen', side_effect=lambda *a, **kw:
                   popen([sys.executable, '-c', 'exit(7)'], start_new_session=True)):
            try:
                self.assertEqual(supervise('run', children, [False], lambda: None), 1)
            finally:
                stop_children(children)
        self.assertEqual(children, [])

    def test_shutdown_kills_stubborn_descendant_group(self):
        with TemporaryDirectory() as directory:
            pidfile = Path(directory) / 'pid'
            code = ('import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                    'pid=os.fork(); '
                    f'open({str(pidfile)!r},"w").write(str(os.getpid())) if pid==0 else None; '
                    'time.sleep(60)')
            process = subprocess.Popen([sys.executable, '-c', code], start_new_session=True)
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(pidfile.exists())
            descendant = int(pidfile.read_text())
            with patch('deploy.supervisor.STOP_TIMEOUT', .1):
                stop_children([process])
            self.assertIsNotNone(process.returncode)
            # An orphan can briefly remain a zombie pending PID 1 reaping;
            # it must never remain running after the bounded shutdown.
            stat = Path(f'/proc/{descendant}/stat')
            deadline = time.monotonic() + 2
            while stat.exists() and stat.read_text().split()[2] != 'Z' and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(not stat.exists() or stat.read_text().split()[2] == 'Z')

    def test_migration_failure_never_starts_working_components(self):
        from django.test import override_settings
        from deploy.supervisor import main
        from unittest.mock import Mock
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': str(root / 'new.sqlite3')}}
            child = Mock()
            child.wait.return_value = 1
            with override_settings(DATABASES=database, MEDIA_ROOT=str(root / 'media')), \
                 patch.dict(os.environ, {'RUNTIME_DIR': str(root / 'runtime'), 'BACKUP_EXPORT_COMMAND': ''}), \
                 patch('deploy.supervisor.signal.signal'), \
                 patch('deploy.supervisor.logging.config.dictConfig'), \
                 patch('deploy.supervisor.subprocess.Popen', return_value=child) as popen:
                self.assertEqual(main(['setup']), 1)
                self.assertEqual(popen.call_count, 1)
                self.assertEqual(popen.call_args.args[0][1:], ['manage.py', 'migrate', '--noinput'])

    def test_daily_snapshot_stops_every_writer_before_backup(self):
        children, stopping = [], [False]
        popen = subprocess.Popen
        stopped = []
        def launch(*args, **kwargs):
            process = popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
            stopped.append(process)
            return process
        def backup():
            self.assertFalse(children)
            self.assertTrue(all(child.poll() is not None for child in stopped))
            stopping[0] = True
        with patch('deploy.supervisor.BACKUP_INTERVAL', .01), \
             patch('deploy.supervisor.subprocess.Popen', side_effect=launch):
            try:
                self.assertEqual(supervise('run', children, stopping, backup), 0)
            finally:
                stop_children(children)


from django.test import TestCase
from deploy.health import mark, ready


class ReadinessTests(TestCase):
    def test_activity_incoming_failure_and_old_outbox_are_detected(self):
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        from datetime import timedelta
        from modules.delivery.models import OutgoingMessage
        get_user_model().objects.create_superuser('teacher', password='fixture')
        with TemporaryDirectory() as directory, patch.dict(os.environ, {'APP_MANAGED': '1', 'RUNTIME_DIR': directory}):
            (Path(directory) / 'mode').write_text('run')
            for name in ('supervisor', 'scheduler', 'polling', 'backup'):
                mark(name)
            self.assertEqual(ready(), [])
            mark('incoming-error-42')
            os.utime(Path(directory) / 'incoming-error-42', (time.time() - 301,) * 2)
            self.assertIn('incoming', ready())
            row = OutgoingMessage.objects.create(bot_id=1, chat_id=1, operation='fixture', part=0,
                next_attempt_at=timezone.now(), created_at=timezone.now() - timedelta(minutes=16))
            self.assertIn('outbox', ready())
            row.state = 'sent'
            row.save()
            (Path(directory) / 'incoming-error-42').unlink()
            self.assertEqual(ready(), [])
            os.utime(Path(directory) / 'polling', (time.time() - 121,) * 2)
            self.assertIn('polling', ready())

    def test_recent_rejected_update_is_reported_for_a_day(self):
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        from datetime import timedelta
        from modules.delivery.models import IncomingEvent
        get_user_model().objects.create_superuser('teacher', password='fixture')
        with TemporaryDirectory() as directory, patch.dict(os.environ, {'APP_MANAGED': '1', 'RUNTIME_DIR': directory}):
            (Path(directory) / 'mode').write_text('run')
            for name in ('supervisor', 'scheduler', 'polling', 'backup'):
                mark(name)
            row = IncomingEvent.objects.create(bot_id=1, update_id=1, received_at=timezone.now(),
                                               processed_at=timezone.now(), state='failed')
            self.assertEqual(ready(), ['incoming_failed'])
            row.processed_at = timezone.now() - timedelta(days=1, seconds=1)
            row.save()
            self.assertEqual(ready(), [])


class SettingsTests(unittest.TestCase):
    def test_every_configuration_takes_the_writer_lock_at_begin(self):
        from django.conf import settings
        from config import settings as base
        self.assertEqual(base.DATABASES['default']['OPTIONS']['transaction_mode'], 'IMMEDIATE')
        self.assertEqual(settings.DATABASES['default']['OPTIONS']['transaction_mode'], 'IMMEDIATE')
