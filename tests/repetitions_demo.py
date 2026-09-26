"""Run with python -m tests.repetitions_demo: only isolated temporary SQLite."""
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.db import connections
from django.db.migrations.executor import MigrationExecutor

from config import repetitions as policy
from modules.repetitions.scheduling import MOSCOW, moscow_date

from modules.users.models import Student, StudentTopic
from modules.users.services import assign_topics
from modules.repetitions.models import TopicProgress
from modules.repetitions.services import apply_result, due_topics


def main():
    print(f'Config: error={policy.ERROR_RETRY_DAYS} days, success={policy.SUCCESS_INTERVAL_DAYS}')
    connections.close_all()
    with TemporaryDirectory(prefix='repetitions-demo-') as directory:
        connection = connections['default']
        connection.settings_dict['NAME'] = str(Path(directory) / 'isolated.sqlite3')
        try:
            call_command('migrate', verbosity=0)
            call_command('migrate', check=True, verbosity=0)
            print('Empty temporary DB: migrate / migrate --check OK')
            executor = MigrationExecutor(connection)
            leaves = executor.loader.graph.leaf_nodes()
            executor.migrate([('repetitions', None)])
            try:
                apps = executor.loader.project_state([('users', '0001_initial')]).apps
                course = apps.get_model('materials', 'Course').objects.create(title='Demo')
                topic = apps.get_model('materials', 'Topic').objects.create(course=course, title='Active')
                inactive = apps.get_model('materials', 'Topic').objects.create(course=course, title='Inactive')
                student = apps.get_model('users', 'Student').objects.create(course=course, telegram_id=123, first_name='Demo')
                now = datetime(2026, 9, 20, 21, tzinfo=timezone.utc)
                old = apps.get_model('users', 'StudentTopic')
                for item, active in ((topic, True), (inactive, False)):
                    old.objects.create(student=student, topic=item, is_active=active, activated_at=now)
            finally:
                MigrationExecutor(connection).migrate(leaves)
            call_command('migrate', check=True, verbosity=0)
            assert TopicProgress.objects.count() == 2
            assert len(due_topics(student.pk, now=now)) == 1
            print('Stage 3 temporary DB: active/inactive assignments migrated OK')
            student = Student.objects.create(course_id=course.pk, telegram_id=124, first_name='Fresh demo')
            assign_topics(student.pk, [topic.pk], now=now)
            assignment = StudentTopic.objects.get(student_id=student.pk, topic_id=topic.pk)
            def show(label, expected_step, expected_date):
                assignment.refresh_from_db()
                progress = assignment.progress
                actual = (progress.interval_step, progress.next_review_date)
                expected = (expected_step, expected_date)
                assert actual == expected, f'{label}: expected {expected}, got {actual}'
                print(f'{label}: active={assignment.is_active}, step={progress.interval_step}, date={progress.next_review_date}')
            assign_topics(student.pk, [topic.pk], now=now)
            step, expected_date = 0, moscow_date(now)
            show('Enable', step, expected_date)
            # Answer on each expected due date. Expectations use the configured
            # intervals directly, independently of the engine's transition helper.
            for correct in (True, True, False, True):
                now = datetime.combine(expected_date, time(12), tzinfo=MOSCOW)
                step = min(step + 1, policy.MAX_STEP) if correct else 0
                days = policy.SUCCESS_INTERVAL_DAYS[step - 1] if correct else policy.ERROR_RETRY_DAYS
                expected_date = moscow_date(now) + timedelta(days=days)
                apply_result(assignment.pk, correct, now=now)
                show(str(moscow_date(now)) + (' correct' if correct else ' wrong'), step, expected_date)
            assign_topics(student.pk, [], now=now)
            show('Disable', step, expected_date)
            assert not due_topics(student.pk, now=now + timedelta(days=days))
            now += timedelta(days=2)
            assign_topics(student.pk, [topic.pk], now=now)
            show('Reenable', 0, moscow_date(now))
            call_command('migrate', verbosity=0)
            show('Repeated migrate', 0, moscow_date(now))
        finally:
            connections.close_all()


if __name__ == '__main__':
    main()
