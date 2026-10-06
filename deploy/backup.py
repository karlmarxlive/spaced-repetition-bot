"""Called only under the supervisor lock with all writers stopped."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone


def create_backup(db, media, directory, *, export=True):
    db, media = Path(db), Path(media)
    if not db.is_file() or not media.is_dir():
        raise RuntimeError('Backup requires an existing database and media directory')
    directory = Path(directory)
    if directory.resolve().is_relative_to(media.resolve()):
        raise ValueError('Backup directory must be outside media')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    archive = directory / f'{name}.tar.gz'
    with tempfile.TemporaryDirectory(dir=directory) as work:
        work = Path(work)
        with sqlite3.connect(db) as source, sqlite3.connect(work / 'db.sqlite3') as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity check failed')
        shutil.copytree(media, work / 'media', dirs_exist_ok=True)
        files = [p for p in work.rglob('*') if p.is_file()]
        manifest = {'revision': os.environ.get('APP_REVISION', 'unknown'), 'files': {
            str(p.relative_to(work)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
        (work / 'manifest.json').write_text(json.dumps(manifest))
        partial = archive.with_suffix('.partial')
        with tarfile.open(partial, 'w:gz') as output:
            for item in ('db.sqlite3', 'media', 'manifest.json'):
                output.add(work / item, arcname=item)
        partial.chmod(0o600)
        partial.replace(archive)
    # JSON argv, no shell or secrets in command-line diagnostics. Destination
    # credentials come from the exporter environment, never from argv.
    command = json.loads(os.environ.get('BACKUP_EXPORT_COMMAND') or '[]')
    if export and command:
        if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
            raise ValueError('BACKUP_EXPORT_COMMAND must be a JSON argv list')
        process = subprocess.Popen([*command, str(archive)], start_new_session=True)
        try:
            code = process.wait(timeout=20)
            if code:
                raise subprocess.CalledProcessError(code, command)
        finally:
            # Cleanup also covers SIGTERM during export and exporter descendants.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    elif export:
        raise RuntimeError('Configure BACKUP_EXPORT_COMMAND for an off-application copy')
    keep = int(os.environ.get('BACKUP_KEEP', '7'))
    if keep < 1:
        raise ValueError('BACKUP_KEEP must be positive')
    for old in sorted(directory.glob('*.tar.gz'))[:-keep]:
        old.unlink()
    return archive


def restore(archive, db, media):
    """Restore into empty destinations; never overwrite a running installation."""
    db, media = Path(db), Path(media)
    if db.exists() or (media.exists() and any(media.iterdir())):
        raise RuntimeError('Restore requires an absent database and empty media')
    db.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=db.parent) as directory:
        work = Path(directory)
        with tarfile.open(archive) as source:
            source.extractall(work, filter='data')
        manifest = json.loads((work / 'manifest.json').read_text())
        actual = {str(p.relative_to(work)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in work.rglob('*') if p.is_file() and p.relative_to(work) != Path('manifest.json')}
        if actual != manifest['files']:
            raise RuntimeError('Backup checksum mismatch')
        with sqlite3.connect(work / 'db.sqlite3') as connection:
            if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Restore integrity check failed')
        media.mkdir(parents=True, exist_ok=True)
        shutil.copytree(work / 'media', media, dirs_exist_ok=True)
        shutil.move(work / 'db.sqlite3', db)
