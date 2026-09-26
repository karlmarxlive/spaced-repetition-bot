from datetime import datetime, date, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction, IntegrityError
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from modules.materials.models import Course, Topic
from modules.users.models import Student, StudentTopic
from modules.users.services import assign_topics
from modules.repetitions.models import TopicProgress
from modules.repetitions.scheduling import calculate_transition, initial_schedule
from modules.repetitions.services import apply_result, due_topics, InconsistentProgressError

NOW = datetime(2026, 9, 20, 21, tzinfo=dt_timezone.utc)


class CalendarTests(SimpleTestCase):
    def test_custom_intervals_and_step_count(self):
        from config import repetitions as policy
        with patch.multiple(policy, ERROR_RETRY_DAYS=2, SUCCESS_INTERVAL_DAYS=(4, 10), MAX_STEP=2):
            first = calculate_transition(0, True, now=NOW)
            self.assertEqual((first.interval_step, first.next_review_date), (1, date(2026, 9, 25)))
            for step in (1, 2):
                result = calculate_transition(step, True, now=NOW)
                self.assertEqual((result.interval_step, result.next_review_date), (2, date(2026, 10, 1)))
            result = calculate_transition(2, False, now=NOW)
            self.assertEqual((result.interval_step, result.next_review_date), (0, date(2026, 9, 23)))
            with self.assertRaises(ValueError):
                calculate_transition(3, True, now=NOW)

    def test_invalid_interval_configuration(self):
        from config.repetitions import validate_intervals
        for error_days, success_days in ((0, (3,)), (True, (3,)), (1, ()),
                                         (1, (0,)), (1, (-3,)), (1, (1.5,)), (1, '3,7')):
            with self.subTest(error_days=error_days, success_days=success_days), self.assertRaises(ValueError):
                validate_intervals(error_days, success_days)

    def test_complete_correct_chain(self):
        step = 0
        now = NOW
        dates = [date(2026, 9, 22), date(2026, 9, 25), date(2026, 10, 2),
                 date(2026, 10, 16), date(2026, 11, 15), date(2027, 1, 14), date(2027, 3, 15)]
        for index, expected in enumerate(dates):
            result = calculate_transition(step, True, now=now)
            self.assertEqual((result.interval_step, result.next_review_date), (min(index + 1, 6), expected))
            step = result.interval_step
            now = datetime.combine(expected, datetime.min.time(), tzinfo=dt_timezone.utc)

    def test_errors_from_every_step_and_recovery(self):
        for step in range(7):
            result = calculate_transition(step, False, now=NOW)
            self.assertEqual((result.interval_step, result.next_review_date), (0, date(2026, 9, 22)))
        for days in range(3):
            result = calculate_transition(0, False, now=NOW + timedelta(days=days))
            self.assertEqual(result.next_review_date, date(2026, 9, 22) + timedelta(days=days))
        self.assertEqual(calculate_transition(0, True, now=NOW).next_review_date, date(2026, 9, 22))

    def test_calendar_boundaries_ignore_active_timezone(self):
        for text, expected in [('2024-02-28T21:00:00+00:00', date(2024, 3, 1)),
                               ('2026-12-31T20:59:59+00:00', date(2027, 1, 1)),
                               ('2026-12-31T21:00:00+00:00', date(2027, 1, 2)),
                               ('2026-04-30T21:00:00+00:00', date(2026, 5, 2))]:
            with timezone.override('America/Los_Angeles'):
                self.assertEqual(calculate_transition(5, False, now=datetime.fromisoformat(text)).next_review_date, expected)
        self.assertEqual(initial_schedule(now=datetime(2024, 2, 28, 21, tzinfo=dt_timezone.utc)).next_review_date, date(2024, 2, 29))

    def test_invalid_inputs(self):
        for step in (-1, 7, True, 1.0, None):
            with self.assertRaises(ValueError):
                calculate_transition(step, True, now=NOW)
        for correct in (None, 'unavailable', 0, 1):
            with self.assertRaises(ValueError):
                calculate_transition(0, correct, now=NOW)
        with self.assertRaisesRegex(ValueError, 'timezone-aware'):
            initial_schedule(now=NOW.replace(tzinfo=None))


class ProgressTests(TestCase):
    def setUp(self):
        self.course = Course.objects.create(title='Course')
        self.topic = Topic.objects.create(course=self.course, title='Topic')
        self.student = Student.objects.create(course=self.course, telegram_id=1, first_name='One')

    def assign(self):
        assign_topics(self.student.pk, [self.topic.pk], now=NOW)
        return StudentTopic.objects.get(student=self.student, topic=self.topic)

    def test_lifecycle_and_late_answer(self):
        row = self.assign()
        self.assertEqual(row.progress.next_review_date, date(2026, 9, 21))
        late = NOW + timedelta(days=40)
        result = apply_result(row.pk, True, now=late)
        self.assertEqual((result.interval_step, result.next_review_date), (1, date(2026, 11, 1)))
        assign_topics(self.student.pk, [self.topic.pk], now=late)
        row.refresh_from_db()
        self.assertEqual(row.activated_at, NOW)
        self.assertEqual(row.progress.interval_step, 1)
        assign_topics(self.student.pk, [], now=late)
        self.assertEqual(due_topics(self.student.pk, now=late + timedelta(days=100)), [])
        with self.assertRaises(ValueError):
            apply_result(row.pk, False, now=late)
        result.refresh_from_db()
        self.assertEqual(result.interval_step, 1)
        assign_topics(self.student.pk, [self.topic.pk], now=late)
        row.refresh_from_db()
        self.assertEqual(row.activated_at, late)
        self.assertEqual(row.pk, self.student.topic_assignments.get().pk)
        self.assertEqual((row.progress.interval_step, row.progress.next_review_date), (0, date(2026, 10, 31)))

    def test_independent_students_and_topics(self):
        row = self.assign()
        other = Student.objects.create(course=self.course, telegram_id=2, first_name='Two')
        topic = Topic.objects.create(course=self.course, title='Two')
        assign_topics(other.pk, [self.topic.pk], now=NOW)
        assign_topics(self.student.pk, [self.topic.pk, topic.pk], now=NOW)
        apply_result(row.pk, True, now=NOW)
        self.assertEqual(TopicProgress.objects.filter(interval_step=0).count(), 2)
        self.assertEqual(TopicProgress.objects.filter(interval_step=1).count(), 1)

    def test_months_of_saved_progress_and_errors_at_each_step(self):
        row = self.assign()
        now = NOW
        dates = [date(2026, 9, 22), date(2026, 9, 25), date(2026, 10, 2),
                 date(2026, 10, 16), date(2026, 11, 15), date(2027, 1, 14), date(2027, 3, 15)]
        for index, expected in enumerate(dates):
            result = apply_result(row.pk, True, now=now)
            saved = TopicProgress.objects.get(pk=result.pk)
            self.assertEqual(saved.interval_step, min(index + 1, 6))
            self.assertEqual(saved.next_review_date, expected)
            now = datetime.combine(expected, datetime.min.time(), tzinfo=dt_timezone.utc)
        for step in range(7):
            assign_topics(self.student.pk, [], now=now)
            assign_topics(self.student.pk, [self.topic.pk], now=now)
            for _ in range(step):
                apply_result(row.pk, True, now=now)
            apply_result(row.pk, False, now=now)
            apply_result(row.pk, False, now=now + timedelta(days=1))
            self.assertEqual(TopicProgress.objects.get(assignment=row).interval_step, 0)
            self.assertEqual(apply_result(row.pk, True, now=now + timedelta(days=2)).interval_step, 1)

    def test_due_boundary_order_and_read_only(self):
        row = self.assign()
        a = Topic.objects.create(course=self.course, title='a', order=2)
        b = Topic.objects.create(course=self.course, title='b', order=2)
        assign_topics(self.student.pk, [self.topic.pk, a.pk, b.pk], now=NOW)
        self.assertEqual(due_topics(self.student.pk, now=NOW - timedelta(seconds=1)), [])
        with CaptureQueriesContext(connection) as queries:
            for _ in range(2):
                self.assertEqual([r.topic_id for r in due_topics(self.student.pk, now=NOW)], [self.topic.pk, a.pk, b.pk])
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        apply_result(row.pk, True, now=NOW)
        self.assertEqual(len(due_topics(self.student.pk, now=NOW)), 2)
        self.assertEqual(len(due_topics(self.student.pk, now=NOW + timedelta(days=100))), 3)

    def test_invalid_operations_do_not_write(self):
        row = self.assign()
        for operation in (lambda: assign_topics(self.student.pk, [], now=NOW.replace(tzinfo=None)),
                          lambda: apply_result(row.pk, True, now=NOW.replace(tzinfo=None)),
                          lambda: apply_result(row.pk, None, now=NOW),
                          lambda: due_topics(self.student.pk, now=NOW.replace(tzinfo=None))):
            with CaptureQueriesContext(connection) as queries, self.assertRaises(ValueError):
                operation()
            self.assertFalse(any(q['sql'].startswith(('UPDATE', 'INSERT', 'DELETE')) for q in queries))
        for ids in ([True], [self.topic.pk, True], ['1'], [-1], [999999]):
            with self.assertRaises(ValidationError):
                assign_topics(self.student.pk, ids, now=NOW)
        with self.assertRaises(StudentTopic.DoesNotExist):
            apply_result(999999, True, now=NOW)

    def test_constraints_and_delete_protection(self):
        row = self.assign()
        TopicProgress.objects.filter(assignment=row).update(interval_step=6)
        self.assertEqual(TopicProgress.objects.get(assignment=row).interval_step, 6)
        for step in (-1, 7):
            with self.assertRaises(IntegrityError), transaction.atomic():
                TopicProgress.objects.filter(assignment=row).update(interval_step=step)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TopicProgress.objects.create(assignment=row, next_review_date=date(2026, 1, 1))
        with self.assertRaises(ProtectedError):
            row.delete()

    def test_corruption_is_reported_not_repaired(self):
        row = StudentTopic.objects.create(student=self.student, topic=self.topic, activated_at=NOW)
        for operation in (lambda: due_topics(self.student.pk, now=NOW),
                          lambda: assign_topics(self.student.pk, [self.topic.pk], now=NOW),
                          lambda: apply_result(row.pk, True, now=NOW)):
            with self.assertRaises(InconsistentProgressError):
                operation()
        self.assertFalse(TopicProgress.objects.exists())

    def test_assignment_rolls_back_create_and_reset_failure(self):
        row = self.assign()
        apply_result(row.pk, True, now=NOW)
        topic = Topic.objects.create(course=self.course, title='New')
        with patch('modules.repetitions.services.TopicProgress.objects.create', side_effect=RuntimeError('write')):
            with self.assertRaises(RuntimeError):
                assign_topics(self.student.pk, [topic.pk], now=NOW)
        self.assertEqual(StudentTopic.objects.count(), 1)
        assign_topics(self.student.pk, [], now=NOW)
        from modules.users.services import _initialize_progress
        def fail_after_write(*args, **kwargs):
            _initialize_progress(*args, **kwargs)
            raise RuntimeError('after reset')
        with patch('modules.users.services._initialize_progress', side_effect=fail_after_write):
            with self.assertRaises(RuntimeError):
                assign_topics(self.student.pk, [self.topic.pk], now=NOW + timedelta(days=1))
        row.refresh_from_db()
        self.assertFalse(row.is_active)
        self.assertEqual(row.activated_at, NOW)
        self.assertEqual(row.progress.interval_step, 1)

    def test_outer_transaction_and_fresh_state(self):
        row = self.assign()
        stale = row.progress
        apply_result(row.pk, True, now=NOW)
        self.assertEqual(stale.interval_step, 0)
        self.assertEqual(apply_result(row.pk, True, now=NOW).interval_step, 2)
        with self.assertRaises(RuntimeError), transaction.atomic():
            apply_result(row.pk, False, now=NOW)
            raise RuntimeError('attempt rollback')
        stale.refresh_from_db()
        self.assertEqual(stale.interval_step, 2)

    def test_admin_lifecycle_and_tampered_post(self):
        self.client.force_login(get_user_model().objects.create_superuser('teacher', password='test'))
        url = reverse('admin:users_student_change', args=[self.student.pk])
        def post(selected, now):
            with patch('modules.users.admin.timezone.now', return_value=now), patch(
                    'modules.users.admin.assign_topics', wraps=assign_topics) as service:
                self.assertEqual(self.client.post(url, {'display_name': 'One', 'completed_topics': selected,
                    'interval_step': 5, 'next_review_date': '2099-01-01'}).status_code, 302)
                service.assert_called_once_with(self.student.pk, selected, now=now)
        post([self.topic.pk], NOW)
        row = self.student.topic_assignments.get()
        apply_result(row.pk, True, now=NOW)
        post([self.topic.pk], NOW + timedelta(days=1))
        row.refresh_from_db()
        self.assertEqual((row.progress.interval_step, row.activated_at), (1, NOW))
        post([], NOW)
        self.assertEqual(due_topics(self.student.pk, now=NOW), [])
        post([self.topic.pk], NOW + timedelta(days=2))
        row.refresh_from_db()
        self.assertEqual((row.progress.interval_step, row.progress.next_review_date), (0, date(2026, 9, 23)))
        self.assertNotIn(TopicProgress, admin.site._registry)


class ProgressMigrationTests(TransactionTestCase):
    def test_stage_three_upgrade_and_repeat_migrate(self):
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate([('repetitions', None)])
            apps = executor.loader.project_state([('users', '0001_initial')]).apps
            course = apps.get_model('materials', 'Course').objects.create(title='Historical')
            topic = apps.get_model('materials', 'Topic').objects.create(course=course, title='Historical')
            student = apps.get_model('users', 'Student').objects.create(course=course, telegram_id=10, first_name='Old')
            second = apps.get_model('materials', 'Topic').objects.create(course=course, title='Disabled')
            old = apps.get_model('users', 'StudentTopic')
            a = old.objects.create(student=student, topic=topic, activated_at=NOW, is_active=True)
            b = old.objects.create(student=student, topic=second, activated_at=NOW - timedelta(seconds=1), is_active=False)
            apps.get_model('users', 'Invitation').objects.create(course=course, token='historical-token')
            task = apps.get_model('materials', 'Task').objects.create(topic=topic, title='Task')
            apps.get_model('materials', 'TaskAttachment').objects.create(task=task, file='tasks/preserved.txt')
            models = [apps.get_model('users', name) for name in ('Student', 'Invitation', 'StudentTopic')]
            models += [apps.get_model('materials', name) for name in ('Course', 'Topic', 'Task', 'TaskAttachment')]
            snapshots = {model._meta.label: list(model.objects.order_by('pk').values()) for model in models}
            executor = MigrationExecutor(connection)
            executor.migrate(leaves)
            current_apps = executor.loader.project_state(leaves).apps
            for label, values in snapshots.items():
                self.assertEqual(list(current_apps.get_model(label).objects.order_by('pk').values()), values)
            self.assertEqual(TopicProgress.objects.get(assignment_id=a.pk).next_review_date, date(2026, 9, 21))
            self.assertEqual(TopicProgress.objects.get(assignment_id=b.pk).next_review_date, date(2026, 9, 20))
            self.assertEqual(StudentTopic.objects.get(pk=a.pk).activated_at, NOW)
            self.assertFalse(StudentTopic.objects.get(pk=b.pk).is_active)
            self.assertEqual([r.pk for r in due_topics(student.pk, now=NOW + timedelta(days=100))], [a.pk])
            apply_result(a.pk, True, now=NOW)
            MigrationExecutor(connection).migrate(leaves)
            self.assertEqual(TopicProgress.objects.get(assignment_id=a.pk).interval_step, 1)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
