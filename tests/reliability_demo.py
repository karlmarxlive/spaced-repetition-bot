"""Disposable file SQLite: races, crash recovery, migration upgrade, new process."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch


def main():
    os.environ['DJANGO_SETTINGS_MODULE'] = 'config.test_settings'
    import django
    django.setup()
    from django.core.management import call_command
    from django.db import connections, connection
    from django.db.migrations.executor import MigrationExecutor
    from modules.delivery.application import process_event, daily_review
    from modules.delivery.models import IncomingEvent, OutgoingMessage, QuestionDelivery
    from modules.delivery.outbox import claim, finish
    from modules.materials.models import Course, Task, Topic
    from modules.repetitions.models import TopicProgress
    from modules.study_sessions.models import Attempt, StudySession, DailyReviewRun, TaskCursor
    from modules.study_sessions.services import start_review, accept_answer, continue_review
    from modules.users.models import Student
    from modules.users.services import assign_topics
    from tests.test_reliability import event, BOT_ID
    from tests.test_daily_reviews import NOW

    with TemporaryDirectory(prefix='stage7-sqlite-') as directory:
        path = Path(directory) / 'isolated.sqlite3'
        connection.close()
        connection.settings_dict['NAME'] = str(path)
        connection.settings_dict['OPTIONS'] = {'timeout': 2, 'transaction_mode': 'IMMEDIATE'}
        try:
            # Start at the actual stage-6 schema, with a repeated topic in a long session.
            executor = MigrationExecutor(connection)
            old = [('study_sessions', '0003_daily_queue'), ('repetitions', '0003_six_intervals')]
            executor.migrate(old)
            course = Course.objects.create(title='Historical')
            student = Student.objects.create(course=course, telegram_id=42, first_name='Student')
            topics = [Topic.objects.create(course=course, title=f'Topic {i}', order=i) for i in range(3)]
            for topic in topics:
                Task.objects.create(topic=topic, title=topic.title, question=topic.title, answer='yes', is_active=True)
            # Historical schema: assignment service's new cancellation hook is not used.
            from modules.users.models import StudentTopic
            for topic in topics:
                assignment = StudentTopic.objects.create(student=student, topic=topic, activated_at=NOW)
                TopicProgress.objects.create(assignment=assignment, next_review_date=NOW.date())
            first = start_review(student.pk, now=NOW).question
            accept_answer(student.pk, first.attempt_id, 'yes', now=NOW)
            second = continue_review(student.pk, now=NOW).question
            daily_review_time = NOW + timedelta(days=1)
            from modules.study_sessions.services import form_daily_review
            form_daily_review(student.pk, now=daily_review_time)
            accept_answer(student.pk, second.attempt_id, 'yes', now=daily_review_time)
            third = continue_review(student.pk, now=daily_review_time).question
            accept_answer(student.pk, third.attempt_id, 'yes', now=daily_review_time)
            repeated = continue_review(student.pk, now=daily_review_time).question
            assert Attempt.objects.get(pk=repeated.attempt_id).assignment_id == Attempt.objects.get(pk=first.attempt_id).assignment_id
            cancelled_student = Student.objects.create(course=course, telegram_id=44, first_name='Cancelled')
            cancelled_assignment = StudentTopic.objects.create(student=cancelled_student, topic=topics[0], activated_at=NOW, is_active=False)
            TopicProgress.objects.create(assignment=cancelled_assignment, next_review_date=NOW.date())
            cancelled_session = StudySession.objects.create(student=cancelled_student, started_at=NOW,
                finished_at=NOW, state='finished')
            cancelled_attempt = Attempt.objects.create(student=cancelled_student, session=cancelled_session,
                assignment=cancelled_assignment, task=Task.objects.filter(topic=topics[0]).get(),
                opened_at=NOW, finished_at=NOW, status='cancelled', question='Old cancelled question',
                reference='old reference', topic_title='Old title', attachments=[], task_order=0)
            from modules.study_sessions.models import ReviewQueueItem
            ReviewQueueItem.objects.create(session=cancelled_session, assignment=cancelled_assignment,
                attempt=cancelled_attempt, enqueued_at=NOW, finished_at=NOW, state='cancelled')
            models = [Attempt, StudySession, TopicProgress, DailyReviewRun, TaskCursor, ReviewQueueItem]
            snapshot = {m: list(m.objects.order_by('pk').values()) for m in models}
            executor = MigrationExecutor(connection)
            leaves = executor.loader.graph.leaf_nodes()
            executor.migrate(leaves)
            for model, rows in snapshot.items():
                assert list(model.objects.order_by('pk').values()) == rows
            assert not OutgoingMessage.objects.exists() and not QuestionDelivery.objects.exists()
            # An unknown legacy delivery is not answerable until /review resends it.
            process_event(event(1), now=daily_review_time)
            assert Attempt.objects.get(pk=repeated.attempt_id).status == 'open'
            assert IncomingEvent.objects.get(update_id=1).attempt_id == repeated.attempt_id
            process_event(event(2, 'review'), now=daily_review_time)
            sent_id = 1000
            while part := claim(bot_id=BOT_ID, now=daily_review_time, chat_id=42):
                sent_id += 1
                finish(part, now=daily_review_time, message_id=sent_id)
            assert QuestionDelivery.objects.get(attempt_id=repeated.attempt_id).state == 'delivered'
            process_event(event(3, message_id=sent_id + 1), now=daily_review_time)
            assert Attempt.objects.get(pk=repeated.attempt_id).status == 'correct'
            with connection.cursor() as cursor:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
                names = {row[0] for row in cursor.fetchall()}
            assert {'attempt_insert_relationships', 'attempt_update_relationships', 'queue_insert_relationships',
                    'queue_update_relationships', 'assignment_history_relationships'} <= names
            call_command('migrate', check=True, verbosity=0)

            # Fresh student and two independent DB connections race on one update.
            student = Student.objects.create(course=course, telegram_id=50, first_name='Race')
            assign_topics(student.pk, [t.pk for t in topics], now=NOW)
            incoming = event(20, 'review', user_id=50)
            barrier = Barrier(2)
            def run_in_thread(operation):
                try:
                    barrier.wait(timeout=5)
                    return operation()
                finally:
                    connections.close_all()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(run_in_thread, [lambda: process_event(incoming, now=NOW)] * 2))
            assert sorted(results) == [False, True]
            assert Attempt.objects.filter(student=student).count() == 1
            assert OutgoingMessage.objects.filter(student=student, is_question=True).count() == 2
            # Competing workers may claim different chats, but never the same part.
            barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                claimed = list(pool.map(run_in_thread, [lambda: claim(bot_id=BOT_ID, now=NOW, chat_id=50)] * 2))
            claimed = [row for row in claimed if row]
            assert len(claimed) == 1
            row = claimed[0]
            later = NOW + timedelta(seconds=61)
            recovered = claim(bot_id=BOT_ID, now=later, chat_id=50)
            assert recovered.pk == row.pk and recovered.lease_token != row.lease_token
            assert not finish(row, now=later, message_id=2001)
            assert finish(recovered, now=later, message_id=2001)
            while row := claim(bot_id=BOT_ID, now=later, chat_id=50):
                finish(row, now=later, message_id=2002)
            response = event(21, user_id=50, message_id=2100)
            barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(run_in_thread, [lambda: process_event(response, now=later)] * 2))
            assert sorted(results) == [False, True]
            assert Attempt.objects.filter(student=student, status='correct').count() == 1
            assert Attempt.objects.filter(student=student, status='open').count() == 1
            assert TopicProgress.objects.get(assignment__student=student, assignment__topic=topics[0]).interval_step == 1

            rapid_student = Student.objects.create(course=course, telegram_id=60, first_name='Rapid')
            assign_topics(rapid_student.pk, [t.pk for t in topics], now=NOW)
            process_event(event(30, 'review', user_id=60), now=NOW)
            while part := claim(bot_id=BOT_ID, now=NOW, chat_id=60):
                finish(part, now=NOW, message_id=3001 + part.part)
            barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(run_in_thread, [lambda: process_event(event(31, user_id=60, message_id=3100), now=NOW),
                    lambda: process_event(event(32, user_id=60, message_id=3101), now=NOW)]))
            assert Attempt.objects.filter(student=rapid_student, status='correct').count() == 1
            assert Attempt.objects.filter(student=rapid_student, status='open').count() == 1

            # Manual command concurrently with scheduler formation keeps one set.
            barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(run_in_thread, [lambda: process_event(event(22, 'review', user_id=50), now=NOW),
                    lambda: daily_review(student.pk, bot_id=BOT_ID, now=NOW)]))
            current = Attempt.objects.get(student=student, status='open')
            assert OutgoingMessage.objects.filter(attempt=current, is_question=True).count() == 2
            # Real SQLite lock retries the entire operation; no partial journal/progress.
            connection.settings_dict['OPTIONS'] = {'timeout': 0.01, 'transaction_mode': 'IMMEDIATE'}
            connection.close()
            with closing(sqlite3.connect(path)) as locker:
                locker.execute('BEGIN IMMEDIATE')
                from django.db import OperationalError
                try:
                    process_event(event(23, 'unsupported', user_id=50), now=NOW)
                except OperationalError:
                    pass
                else:
                    raise AssertionError('Expected bounded lock failure')
                locker.rollback()
            assert not IncomingEvent.objects.filter(update_id=23).exists()
            process_event(event(23, 'unsupported', user_id=50), now=NOW)
            # Refuse a lossy reverse migration once transport history exists.
            try:
                MigrationExecutor(connection).migrate([('delivery', None)])
            except RuntimeError as error:
                assert 'Delivery history exists' in str(error)
            else:
                raise AssertionError('Lossy reverse should be rejected')
            call_command('migrate', check=True, verbosity=0)
            connections.close_all()
            # New interpreter opens the same file and replays an accepted update.
            script = '''import os, sys
os.environ['DJANGO_SETTINGS_MODULE']='config.test_settings'
import django; django.setup()
from django.db import connection
connection.settings_dict['NAME']=sys.argv[1]
from modules.delivery.application import process_event
from modules.delivery.models import OutgoingMessage,QuestionDelivery
from modules.delivery.outbox import claim,finish
from modules.study_sessions.models import Attempt
from tests.test_reliability import event
from tests.test_daily_reviews import NOW
before=OutgoingMessage.objects.count()
assert not process_event(event(21,user_id=50,message_id=2100),now=NOW)
assert OutgoingMessage.objects.count()==before
assert Attempt.objects.filter(student__telegram_id=50,status='correct').count()==1
assert Attempt.objects.filter(student__telegram_id=50,status='open').count()==1
from datetime import timedelta
recovery_now=NOW+timedelta(seconds=61)
index=5000
while part:=claim(bot_id=123456789,now=recovery_now,chat_id=50):
    finish(part,now=recovery_now,message_id=index)
    index+=1
assert QuestionDelivery.objects.get(attempt__student__telegram_id=50,attempt__status='open').state=='delivered'
'''
            result = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stdout + result.stderr
            # Also apply the entire graph directly to a second empty file DB.
            connection.close()
            connection.settings_dict['NAME'] = str(Path(directory) / 'empty.sqlite3')
            call_command('migrate', verbosity=0)
            call_command('migrate', check=True, verbosity=0)
            assert not Attempt.objects.exists()
            assert not OutgoingMessage.objects.exists()
        finally:
            connections.close_all()
    print('Stage 7 SQLite recovery: OK')


if __name__ == '__main__':
    main()
