"""Subprocess-only checks against an isolated file DB, never the working SQLite."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
from datetime import datetime, timezone

from django.core.management import call_command
from django.db import connections, OperationalError

from modules.materials.models import Course, Task, Topic
from modules.users.models import Invitation, Student
from modules.users.services import register_student, revoke_invitation, assign_topics
from modules.repetitions.services import apply_result
from modules.repetitions.models import TopicProgress
from modules.study_sessions.models import Attempt, DailyReviewRun, ReviewQueueItem
from modules.study_sessions.services import accept_answer, form_daily_review, start_review
from tests.support import current_question


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

        # Revocation and registration must have exactly one winner.
        invitation = Invitation.objects.create(course=course)
        barrier = Barrier(2)

        def claim_or_revoke(claim):
            try:
                barrier.wait(timeout=5)
                if claim:
                    return register_student(telegram_id=6, first_name="Test", payload=invitation.token)
                return revoke_invitation(invitation.pk)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            registered, revoked = list(pool.map(claim_or_revoke, [True, False]))
        invitation.refresh_from_db()
        assert (registered.status, revoked) in {("registered", False), ("unavailable", True)}
        assert invitation.is_revoked == revoked
        assert (invitation.used_at is None) == revoked
        assert (invitation.student_id is None) == revoked
        assert Student.objects.filter(telegram_id=6).exists() == (not revoked)

        # Two accepted answers serialize on real SQLite; neither reads stale step 0.
        now = datetime(2026, 9, 20, tzinfo=timezone.utc)
        student = Student.objects.get(telegram_id=3)
        topic = Topic.objects.create(course=course, title="Repeat")
        assign_topics(student.pk, [topic.pk], now=now)
        assignment = student.topic_assignments.get()
        connection.settings_dict["OPTIONS"] = {"timeout": 5}
        connection.close()
        barrier = Barrier(2)

        def answer(_):
            try:
                barrier.wait(timeout=5)
                return apply_result(assignment.pk, True, now=now).interval_step
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(answer, range(2))) == [1, 2]
        assert TopicProgress.objects.get(assignment=assignment).interval_step == 2

        # Result vs unchanged assignment also preserves the advanced state.
        barrier = Barrier(2)
        def answer_or_assign(answering):
            try:
                barrier.wait(timeout=5)
                if answering:
                    apply_result(assignment.pk, True, now=now)
                else:
                    assign_topics(student.pk, [topic.pk], now=now)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(answer_or_assign, [True, False]))
        assert TopicProgress.objects.get(assignment=assignment).interval_step == 3

        connection.settings_dict["OPTIONS"] = {"timeout": 0.05}
        connection.close()
        with closing(sqlite3.connect(path, timeout=0.05)) as locker:
            locker.execute("BEGIN IMMEDIATE")
            try:
                for operation in (lambda: apply_result(assignment.pk, False, now=now),
                                  lambda: assign_topics(student.pk, [], now=now)):
                    try:
                        operation()
                    except OperationalError as error:
                        assert "locked" in str(error).lower()
                    else:
                        raise AssertionError("Expected a real SQLite lock failure")
            finally:
                locker.rollback()
        assignment.refresh_from_db()
        assert assignment.is_active
        assert TopicProgress.objects.get(assignment=assignment).interval_step == 3
        assert apply_result(assignment.pk, False, now=now).interval_step == 0

        # Two independent scheduler/manual writers share a persisted queue.
        connection.settings_dict["OPTIONS"] = {"timeout": 5}
        connection.close()
        student = Student.objects.create(course=course, telegram_id=20, first_name="Daily")
        topics = [Topic.objects.create(course=course, title=f"Daily {i}", order=i) for i in range(2)]
        for topic in topics:
            Task.objects.create(topic=topic, title=topic.title, question=topic.title, answer="yes", is_active=True)
        now = now.replace(hour=15)
        assign_topics(student.pk, [t.pk for t in topics], now=now)

        def race_review(daily):
            try:
                barrier.wait(timeout=5)
                operation = form_daily_review if daily else start_review
                return operation(student.pk, now=now).status
            finally:
                connections.close_all()

        for sources in ([True, False], [True, True]):
            barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(race_review, sources))
            assert ReviewQueueItem.objects.filter(assignment__student=student).count() == 2
            assert Attempt.objects.filter(student=student, status="open").count() == 1
            assert DailyReviewRun.objects.filter(student=student).count() == 1

        connections.close_all()  # Reopen from disk, not an in-memory queue.
        question = current_question(student.pk)
        barrier = Barrier(2)

        def duplicate_answer(_):
            try:
                barrier.wait(timeout=5)
                return accept_answer(student.pk, question.attempt_id, "yes", now=now).status
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(duplicate_answer, range(2))) == ["closed", "correct"]
        assert TopicProgress.objects.get(assignment__student=student, assignment__topic=topics[0]).interval_step == 1
        assert ReviewQueueItem.objects.get(attempt_id=question.attempt_id).state == "done"
        assert start_review(student.pk, now=now).question.text == topics[1].title
        connections.close_all()
    print("SQLite races and lock recovery: OK")


if __name__ == "__main__":
    main()
