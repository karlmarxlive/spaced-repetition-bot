"""Subprocess-only checks against an isolated file DB, never the working SQLite."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier

from django.core.management import call_command
from django.db import connections

from modules.materials.models import Course
from modules.users.models import Invitation, Student
from modules.users.services import register_student


def main():
    connections.close_all()
    with TemporaryDirectory(prefix="registration-sqlite-") as directory:
        path = str(Path(directory) / "test.sqlite3")
        connection = connections["default"]
        connection.settings_dict["NAME"] = path
        connection.settings_dict["OPTIONS"] = {"timeout": 0.05}
        call_command("migrate", verbosity=0)
        course = Course.objects.create(title="Concurrency")

        def race(calls):
            barrier = Barrier(len(calls))

            def worker(args):
                try:
                    barrier.wait(timeout=5)
                    return register_student(first_name="Test", **args)
                finally:
                    connections.close_all()

            with ThreadPoolExecutor(max_workers=len(calls)) as pool:
                return list(pool.map(worker, calls))

        # Two identities compete for one invitation.
        invitation = Invitation.objects.create(course=course)
        results = race([{"telegram_id": i, "payload": invitation.token} for i in (1, 2)])
        assert sorted(r.status for r in results) == ["registered", "unavailable"], results
        assert Student.objects.count() == 1
        invitation.refresh_from_db()
        assert invitation.student_id is not None and invitation.used_at is not None

        # One identity competes with itself for different invitations.
        invites = [Invitation.objects.create(course=course) for _ in range(2)]
        results = race([{"telegram_id": 3, "payload": item.token} for item in invites])
        assert sorted(r.status for r in results) == ["existing", "registered"], results
        assert Student.objects.filter(telegram_id=3).count() == 1
        assert Invitation.objects.filter(pk__in=[i.pk for i in invites], used_at__isnull=False).count() == 1

        # Exact duplicate update arriving concurrently.
        invitation = Invitation.objects.create(course=course)
        results = race([{"telegram_id": 4, "payload": invitation.token}] * 2)
        assert sorted(r.status for r in results) == ["existing", "registered"], results
        assert results[0].student_id == results[1].student_id

        # Actual SQLite lock, not a mocked OperationalError; no lost invitation.
        invitation = Invitation.objects.create(course=course)
        with closing(sqlite3.connect(path, timeout=0.05)) as locker:
            locker.execute("BEGIN IMMEDIATE")
            try:
                result = register_student(telegram_id=5, first_name="Test", payload=invitation.token)
                assert result.status == "busy", result
            finally:
                locker.rollback()
        invitation.refresh_from_db()
        assert invitation.used_at is None
        assert not Student.objects.filter(telegram_id=5).exists()
        assert register_student(telegram_id=5, first_name="Test", payload=invitation.token).status == "registered"
        connections.close_all()
    print("SQLite races and lock recovery: OK")


if __name__ == "__main__":
    main()
