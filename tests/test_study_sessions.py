from dataclasses import asdict
from datetime import date, timedelta
from importlib import reload
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from modules.checking.services import CheckResult, check_exact
from modules.materials.models import Course, Task, TaskAttachment, Topic
from modules.repetitions.models import TopicProgress
from modules.study_sessions import services
from modules.study_sessions.models import Attempt, StudySession, TaskCursor
from modules.study_sessions.services import accept_answer, current_question, start_review
from modules.users.models import Student, StudentTopic
from modules.users.services import assign_topics
from tests.test_repetitions import NOW


class CheckingTests(SimpleTestCase):
    def test_exact_comparison(self):
        for response, reference, expected in [
            (' \tABC\n', 'ABC ', 'correct'), ('abc', 'ABC', 'incorrect'),
            ('a  b', 'a b', 'incorrect'), ('01', '1', 'incorrect'), ('1.0', '1', 'incorrect'),
            ('é', 'e\u0301', 'incorrect'), ('a.', 'a', 'incorrect'), ('anything', ' ', 'unavailable'),
        ]:
            with self.subTest(response=response):
                self.assertEqual(check_exact(response, reference).status, expected)


class ReviewTests(TestCase):
    def setUp(self):
        self.course = Course.objects.create(title='Course')
        self.student = Student.objects.create(course=self.course, telegram_id=42, first_name='One')
        self.topic = Topic.objects.create(course=self.course, title='First')
        self.task = self.make_task(self.topic, 'First question')
        assign_topics(self.student.pk, [self.topic.pk], now=NOW)
        self.assignment = StudentTopic.objects.get(student=self.student)

    def make_task(self, topic, question, order=0):
        return Task.objects.create(topic=topic, title=question[:20], question=question,
                                   answer='reference-secret', is_active=True, order=order)

    def open(self, now=NOW):
        return start_review(self.student.pk, now=now)

    def accept(self, question, text='reference-secret', now=NOW, **kwargs):
        return accept_answer(self.student.pk, question.attempt_id, text, now=now, **kwargs)

    def test_full_cycle_dynamic_candidates_and_skips(self):
        no_tasks = Topic.objects.create(course=self.course, title='Empty', order=1)
        disabled = Topic.objects.create(course=self.course, title='Disabled tasks', order=2)
        Task.objects.create(topic=disabled, title='Inactive')
        assign_topics(self.student.pk, [self.topic.pk, no_tasks.pk, disabled.pk], now=NOW)
        question = self.open().question
        self.assertEqual(self.accept(question).status, 'correct')
        # Topics assigned during the session participate at the next transition.
        later = Topic.objects.create(course=self.course, title='Later', order=3)
        self.make_task(later, 'Second question')
        assign_topics(self.student.pk, [self.topic.pk, no_tasks.pk, disabled.pk, later.pk], now=NOW)
        review = self.open()
        self.assertEqual(len(review.notices), 2)
        self.assertEqual(review.question.text, 'Second question')
        self.assertEqual(self.accept(review.question, 'wrong').status, 'incorrect')
        result = self.open()
        self.assertEqual(asdict(result.summary), dict(correct=1, incorrect=1, skipped=2, cancelled=0))
        self.assertEqual(Attempt.objects.count(), 2)
        self.assertEqual(TopicProgress.objects.get(assignment=self.assignment).next_review_date, date(2026, 9, 22))
        self.assertEqual(TopicProgress.objects.get(assignment__topic=no_tasks).interval_step, 0)

    def test_empty_future_disabled_and_only_skipped(self):
        self.assertEqual(self.open(now=NOW - timedelta(days=1)).summary.correct, 0)
        assign_topics(self.student.pk, [], now=NOW)
        self.assertEqual(self.open().summary.skipped, 0)
        assign_topics(self.student.pk, [self.topic.pk], now=NOW)
        self.task.is_active = False
        self.task.save()
        result = self.open()
        self.assertIsNone(result.question)
        self.assertEqual(result.summary.skipped, 1)
        self.assertEqual(len(result.notices), 1)

    def test_overdue_and_midnight_resume_and_fresh_module(self):
        question = self.open(NOW + timedelta(days=10)).question
        self.assertEqual(self.open(NOW + timedelta(days=20)).question, question)
        reloaded = reload(services)
        self.assertEqual(asdict(reloaded.current_question(self.student.pk)), asdict(question))
        self.assertEqual(Attempt.objects.count(), 1)
        self.assertEqual(TopicProgress.objects.get(assignment=self.assignment).next_review_date, date(2026, 9, 21))
        self.accept(question, now=NOW + timedelta(days=20))
        self.assertEqual(TopicProgress.objects.get(assignment=self.assignment).next_review_date, date(2026, 10, 12))
        self.assertEqual(self.open(NOW + timedelta(days=20)).summary.correct, 1)

    def test_snapshots_survive_edit_move_deactivate_and_attachment_changes(self):
        first = TaskAttachment.objects.create(task=self.task, file='tasks/one/same.txt')
        TaskAttachment.objects.create(task=self.task, file='tasks/two/same.txt')
        question = self.open().question
        other = Topic.objects.create(course=self.course, title='Other')
        self.task.topic, self.task.question, self.task.answer, self.task.is_active = other, 'changed', 'new', False
        self.task.save()
        self.topic.title = 'Renamed'
        self.topic.save()
        first.delete()
        TaskAttachment.objects.filter(task=self.task).update(file='tasks/replaced.txt')
        resumed = self.open().question
        self.assertEqual(resumed, question)
        self.assertEqual(resumed.topic, 'First')
        self.assertEqual([a.path for a in resumed.attachments], ['tasks/one/same.txt', 'tasks/two/same.txt'])
        self.assertNotIn('reference-secret', repr(asdict(resumed)))
        self.assertEqual(self.accept(question).status, 'correct')
        self.assertEqual(TopicProgress.objects.get(assignment=self.assignment).interval_step, 1)
        with self.assertRaises(ProtectedError):
            self.task.delete()

    def test_round_robin_wrong_answer_resume_and_wrap(self):
        second = self.make_task(self.topic, 'Second', order=1)
        third = self.make_task(self.topic, 'Third', order=1)
        now = NOW
        for task in [self.task, second, third, self.task]:
            question = self.open(now).question
            self.assertEqual(Attempt.objects.get(pk=question.attempt_id).task_id, task.pk)
            self.assertEqual(self.open(now).question, question)
            self.accept(question, 'wrong', now=now)
            self.assertEqual(self.open(now).summary.incorrect, 1)
            now += timedelta(days=1)

    def test_cursor_move_disable_reorder_and_add(self):
        second = self.make_task(self.topic, 'Second', 2)
        third = self.make_task(self.topic, 'Third', 3)
        q = self.open().question
        self.accept(q, 'wrong')
        self.open()
        self.task.topic = Topic.objects.create(course=self.course, title='Moved')
        self.task.save()
        q = self.open(NOW + timedelta(days=1)).question
        self.assertEqual(q.text, second.question)
        self.accept(q, 'wrong', NOW + timedelta(days=1))
        self.open(NOW + timedelta(days=1))
        second.is_active = False
        second.save()
        q = self.open(NOW + timedelta(days=2)).question
        self.assertEqual(q.text, third.question)
        self.accept(q, 'wrong', NOW + timedelta(days=2))
        self.open(NOW + timedelta(days=2))
        added = self.make_task(self.topic, 'Added', 4)
        third.order = 5
        third.save()
        q = self.open(NOW + timedelta(days=3)).question
        self.assertEqual(q.text, added.question)  # current third is last, wrap

    def test_single_task_and_students_and_topics_are_independent(self):
        other = Student.objects.create(course=self.course, telegram_id=43, first_name='Two')
        assign_topics(other.pk, [self.topic.pk], now=NOW)
        q = self.open().question
        self.accept(q, 'wrong')
        self.open()
        self.assertEqual(start_review(other.pk, now=NOW).question.text, self.task.question)
        q = self.open(NOW + timedelta(days=1)).question
        self.assertEqual(q.text, self.task.question)
        self.assertEqual(accept_answer(other.pk, q.attempt_id, 'x', now=NOW).status, 'no_attempt')

    def test_invalid_empty_unavailable_and_error_do_not_advance(self):
        q = self.open().question
        for text in ['', ' \t\n', '/unknown', None]:
            self.assertEqual(self.accept(q, text).status, 'text_required')
        self.assertEqual(self.accept(q, checker=lambda *_: CheckResult('unavailable')).status, 'unavailable')
        def fail(*_):
            raise RuntimeError('checker unavailable')
        with self.assertRaises(RuntimeError):
            self.accept(q, checker=fail)
        self.assertEqual(current_question(self.student.pk), q)
        self.assertEqual(TopicProgress.objects.get(assignment=self.assignment).interval_step, 0)

    def test_duplicate_completion_and_atomic_rollback(self):
        q = self.open().question
        with patch('modules.study_sessions.services.apply_result', side_effect=RuntimeError('write')):
            with self.assertRaises(RuntimeError):
                self.accept(q)
        attempt = Attempt.objects.get(pk=q.attempt_id)
        self.assertEqual(attempt.status, 'open')
        self.assertIsNone(attempt.response)
        self.assertFalse(TaskCursor.objects.exists())
        with patch('modules.study_sessions.services.TaskCursor.objects.update_or_create', side_effect=RuntimeError('cursor')):
            with self.assertRaises(RuntimeError):
                self.accept(q)
        self.assertEqual(Attempt.objects.get(pk=q.attempt_id).status, 'open')
        self.assertEqual(TopicProgress.objects.get(assignment=self.assignment).interval_step, 0)
        self.accept(q)
        self.assertEqual(self.accept(q, now=NOW + timedelta(days=50)).status, 'closed')
        self.assertEqual(TopicProgress.objects.get(assignment=self.assignment).interval_step, 1)
        self.assertEqual(TopicProgress.objects.get(assignment=self.assignment).next_review_date, date(2026, 9, 22))

    def test_admin_disable_late_answer_reenable_and_unchanged_save(self):
        self.make_task(self.topic, 'Second', 1)
        q = self.open().question
        self.accept(q, 'wrong')
        self.open()
        q = self.open(NOW + timedelta(days=1)).question
        self.assertEqual(q.text, 'Second')
        self.client.force_login(get_user_model().objects.create_superuser('teacher', password='test'))
        url = reverse('admin:users_student_change', args=[self.student.pk])
        def save(topics):
            with patch('modules.users.admin.timezone.now', return_value=NOW + timedelta(days=1)):
                self.assertEqual(self.client.post(url, {'display_name': 'One', 'completed_topics': topics}).status_code, 302)
        save([self.topic.pk])
        self.assertEqual(current_question(self.student.pk), q)
        self.assertEqual(TaskCursor.objects.get().last_task_id, self.task.pk)
        save([])
        self.assertEqual(self.accept(q).status, 'cancelled')
        self.assertEqual(TopicProgress.objects.get(assignment=self.assignment).next_review_date, date(2026, 9, 22))
        result = self.open(NOW + timedelta(days=1))
        self.assertEqual(result.summary.cancelled, 1)
        self.assertIn('отменено', result.notices[0])
        save([self.topic.pk])
        self.assertEqual(self.open(NOW + timedelta(days=1)).question.text, self.task.question)
        self.assertEqual(Attempt.objects.filter(status='cancelled').count(), 1)

    def test_cancellation_failure_rolls_back_assignment(self):
        q = self.open().question
        from modules.study_sessions.lifecycle import cancel_assignments
        def fail(ids, *, now):
            cancel_assignments(ids, now=now)
            raise RuntimeError('after cancellation')
        with patch('modules.users.services.cancel_assignments', side_effect=fail), self.assertRaises(RuntimeError):
            assign_topics(self.student.pk, [], now=NOW)
        self.assignment.refresh_from_db()
        self.assertTrue(self.assignment.is_active)
        self.assertEqual(current_question(self.student.pk), q)

    def test_database_constraints_and_ownership(self):
        q = self.open().question
        attempt = Attempt.objects.get(pk=q.attempt_id)
        other = Student.objects.create(course=self.course, telegram_id=44, first_name='Other')
        other_session = StudySession.objects.create(student=other, started_at=NOW)
        data = {field.attname: getattr(attempt, field.attname) for field in Attempt._meta.fields if field.name != 'id'}
        for changes in [{}, {'student_id': other.pk, 'session_id': other_session.pk}, {'session_id': other_session.pk},
                        {'status': 'correct'}, {'assignment_id': None}]:
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                Attempt.objects.create(**{**data, **changes})
        with self.assertRaises(IntegrityError), transaction.atomic():
            StudySession.objects.create(student=self.student, started_at=NOW)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StudySession.objects.filter(pk=attempt.session_id).update(state='finished', finished_at=NOW)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StudentTopic.objects.filter(pk=self.assignment.pk).update(student=other)
        # Distinct valid assignment: only the per-student open-attempt index can reject this.
        topic = Topic.objects.create(course=self.course, title='Second')
        task = self.make_task(topic, 'Other task')
        assign_topics(self.student.pk, [self.topic.pk, topic.pk], now=NOW)
        assignment = StudentTopic.objects.get(student=self.student, topic=topic)
        with self.assertRaisesRegex(IntegrityError, 'student_id'), transaction.atomic():
            Attempt.objects.create(**{**data, 'assignment_id': assignment.pk, 'task_id': task.pk})

    def test_newly_due_after_midnight_and_disabled_waiting_topic(self):
        later = Topic.objects.create(course=self.course, title='Tomorrow', order=1)
        disabled = Topic.objects.create(course=self.course, title='Disable before opening', order=2)
        self.make_task(later, 'Tomorrow question')
        self.make_task(disabled, 'Must not open')
        assign_topics(self.student.pk, [self.topic.pk, later.pk, disabled.pk], now=NOW)
        TopicProgress.objects.filter(assignment__topic=later).update(next_review_date=date(2026, 9, 22))
        q = self.open().question
        assign_topics(self.student.pk, [self.topic.pk, later.pk], now=NOW)
        self.accept(q, now=NOW + timedelta(days=1))
        self.assertEqual(TopicProgress.objects.get(assignment=self.assignment).next_review_date, date(2026, 9, 23))
        q = self.open(NOW + timedelta(days=1)).question
        self.assertEqual(q.text, 'Tomorrow question')
        self.accept(q, now=NOW + timedelta(days=1))
        self.assertEqual(self.open(NOW + timedelta(days=1)).summary.correct, 2)
        self.assertFalse(Attempt.objects.filter(assignment__topic=disabled).exists())

    def test_skipped_topic_is_not_revisited_in_same_session(self):
        self.task.is_active = False
        self.task.save()
        later = Topic.objects.create(course=self.course, title='Later', order=1)
        self.make_task(later, 'Later question')
        assign_topics(self.student.pk, [self.topic.pk, later.pk], now=NOW)
        q = self.open().question
        self.task.is_active = True
        self.task.save()
        self.accept(q)
        self.assertEqual(self.open().summary.skipped, 1)
        self.assertEqual(self.open().question.text, self.task.question)

    def test_reactivation_during_session_keeps_history_without_second_topic_attempt(self):
        q = self.open().question
        assign_topics(self.student.pk, [], now=NOW)
        assign_topics(self.student.pk, [self.topic.pk], now=NOW)
        self.assertEqual(self.accept(q).status, 'cancelled')
        self.assertEqual(self.open().summary.cancelled, 1)
        new = self.open().question
        self.assertEqual(new.text, q.text)
        self.assertNotEqual(new.attempt_id, q.attempt_id)

    def test_explicit_aware_time_required(self):
        with self.assertRaises(ValueError):
            self.open(NOW.replace(tzinfo=None))
        self.assertFalse(StudySession.objects.exists())
        q = self.open().question
        with self.assertRaises(ValueError):
            self.accept(q, now=NOW.replace(tzinfo=None))
        self.assertEqual(current_question(self.student.pk), q)
