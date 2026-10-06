import asyncio
import io
import os
from datetime import datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.types import Message
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command, CommandError
from django.db import connection, IntegrityError, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from modules.delivery.scheduler import run_scheduler, run_tick
from modules.materials.models import Course, Task, TaskAttachment, Topic
from modules.repetitions.models import TopicProgress
from modules.study_sessions.models import Attempt, DailyReviewRun, DailySchedule, ReviewQueueItem, StudySession
from modules.study_sessions.services import accept_answer, continue_review, form_daily_review, start_review
from modules.users.models import Student
from modules.users.services import assign_topics
from tests.support import current_question

NOW = datetime(2026, 9, 26, 15, tzinfo=timezone.utc)  # 18:00 Moscow


class DailyFixture:
    def setUp(self):
        DailySchedule.objects.update_or_create(pk=1, defaults={'delivery_time': time(18)})
        self.course = Course.objects.create(title='Course')
        self.student = Student.objects.create(course=self.course, telegram_id=42, first_name='Student')
        self.topics = [Topic.objects.create(course=self.course, title=f'Topic {i}', order=i) for i in range(2)]
        self.tasks = [Task.objects.create(topic=t, title=t.title, question=f'Question {t.order}',
                                         answer='secret-reference', is_active=True) for t in self.topics]
        assign_topics(self.student.pk, [t.pk for t in self.topics], now=NOW)

    def daily(self, now=NOW):
        return form_daily_review(self.student.pk, now=now)

    def answer(self, question, now=NOW):
        return accept_answer(self.student.pk, question.attempt_id, 'secret-reference', now=now)


class DailyReviewTests(DailyFixture, TestCase):
    def test_bound_continuation_returns_finished_summary_without_touching_new_session(self):
        before = NOW - timedelta(minutes=1)
        first = start_review(self.student.pk, now=before).question
        accepted = self.answer(first, before)
        session_id = Attempt.objects.get(pk=first.attempt_id).session_id
        self.assertEqual(accepted.session_id, session_id)
        second = continue_review(self.student.pk, session_id=session_id, now=before).question
        accepted = accept_answer(self.student.pk, second.attempt_id, 'wrong', now=before)
        self.assertEqual(accepted.session_id, session_id)
        finished = self.daily()
        session = StudySession.objects.get(pk=session_id)
        progress = list(TopicProgress.objects.order_by('pk').values())
        for now in (NOW, NOW + timedelta(minutes=1)):
            self.assertEqual(continue_review(self.student.pk, session_id=session_id, now=now), finished)
        self.assertEqual(StudySession.objects.count(), 1)
        session.refresh_from_db()
        self.assertEqual(session.finished_at, NOW)
        new = start_review(self.student.pk, now=NOW + timedelta(days=1))
        self.assertIsNotNone(new.question)
        self.assertEqual(continue_review(self.student.pk, session_id=session_id,
                                        now=NOW + timedelta(days=1)), finished)
        self.assertEqual(current_question(self.student.pk), new.question)
        self.assertEqual(StudySession.objects.count(), 2)
        self.assertEqual(list(TopicProgress.objects.order_by('pk').values()), progress)

    def test_continuation_never_creates_or_uses_another_students_session(self):
        self.assertEqual(continue_review(self.student.pk, now=NOW).status, 'idle')
        self.assertFalse(StudySession.objects.exists())
        other = Student.objects.create(course=self.course, telegram_id=43, first_name='Other')
        session = StudySession.objects.create(student=other, started_at=NOW)
        self.assertEqual(continue_review(self.student.pk, session_id=session.pk, now=NOW).status, 'idle')
        self.assertEqual(StudySession.objects.count(), 1)
        session.refresh_from_db()
        self.assertEqual(session.state, 'open')

    def test_time_boundary_repeat_and_naive_time(self):
        self.assertEqual(self.daily(NOW - timedelta(microseconds=1)).status, 'idle')
        self.assertFalse(DailyReviewRun.objects.exists())
        self.assertFalse(ReviewQueueItem.objects.exists())
        with self.assertRaises(ValueError):
            self.daily(NOW.replace(tzinfo=None))
        question = self.daily().question
        self.assertEqual(question.text, 'Question 0')
        self.assertEqual(self.daily().status, 'idle')
        self.assertEqual(ReviewQueueItem.objects.count(), 2)
        self.assertEqual(DailyReviewRun.objects.get().local_date, NOW.date())

    def test_long_downtime_is_one_batch_not_one_per_missed_day(self):
        self.daily(NOW + timedelta(days=12))
        self.assertEqual(DailyReviewRun.objects.count(), 1)
        self.assertEqual(ReviewQueueItem.objects.count(), 2)
        self.assertEqual(Attempt.objects.count(), 1)

    def test_new_topic_after_daily_waits_but_manual_can_append_it(self):
        first = self.daily().question
        later = Topic.objects.create(course=self.course, title='Late')
        Task.objects.create(topic=later, title='Late', question='Late question', answer='x', is_active=True)
        assign_topics(self.student.pk, [t.pk for t in self.topics] + [later.pk], now=NOW + timedelta(minutes=1))
        self.daily(NOW + timedelta(hours=1))
        self.answer(first)
        second = continue_review(self.student.pk, now=NOW).question
        self.answer(second)
        self.assertEqual(continue_review(self.student.pk, now=NOW).status, 'finished')
        self.assertFalse(ReviewQueueItem.objects.filter(assignment__topic=later).exists())
        self.assertEqual(start_review(self.student.pk, now=NOW).question.text, 'Late question')

    def test_manual_before_daily_and_no_repeat_of_open_question(self):
        first = start_review(self.student.pk, now=NOW - timedelta(hours=1)).question
        self.assertEqual(self.daily().status, 'idle')
        self.assertEqual(current_question(self.student.pk), first)
        self.assertEqual(ReviewQueueItem.objects.count(), 2)
        self.assertEqual(DailyReviewRun.objects.count(), 1)

    def test_same_topic_can_repeat_inside_multi_day_session(self):
        first = self.daily().question
        self.answer(first)
        second = continue_review(self.student.pk, now=NOW).question
        tomorrow = NOW + timedelta(days=1)
        self.assertEqual(self.daily(tomorrow).status, 'idle')
        self.assertEqual(current_question(self.student.pk), second)
        self.assertEqual(ReviewQueueItem.objects.filter(state='pending').count(), 1)
        self.answer(second, tomorrow)
        repeated = continue_review(self.student.pk, now=tomorrow).question
        self.assertEqual(repeated.text, first.text)
        self.assertNotEqual(repeated.attempt_id, first.attempt_id)
        self.assertEqual(StudySession.objects.count(), 1)
        self.answer(repeated, tomorrow)
        self.assertEqual(continue_review(self.student.pk, now=tomorrow).summary.correct, 3)
        self.assertEqual(accept_answer(self.student.pk, repeated.attempt_id, 'x', now=tomorrow).status, 'closed')
        self.assertEqual(TopicProgress.objects.get(assignment__topic=self.topics[0]).interval_step, 2)

    def test_no_duplicate_queue_over_several_days_without_answer(self):
        first = self.daily().question
        for days in (1, 2, 9):
            self.assertEqual(self.daily(NOW + timedelta(days=days)).status, 'idle')
        self.assertEqual(current_question(self.student.pk), first)
        self.assertEqual(ReviewQueueItem.objects.count(), 2)
        self.assertEqual(Attempt.objects.count(), 1)

    def test_disable_cancels_pending_and_open_reenable_preserves_history(self):
        first = self.daily().question
        assign_topics(self.student.pk, [], now=NOW)
        self.assertEqual(ReviewQueueItem.objects.filter(state='cancelled').count(), 2)
        self.assertEqual(self.answer(first).status, 'cancelled')
        assign_topics(self.student.pk, [t.pk for t in self.topics], now=NOW)
        new = self.daily(NOW + timedelta(days=1)).question
        self.assertNotEqual(new.attempt_id, first.attempt_id)
        self.assertEqual(ReviewQueueItem.objects.filter(state='cancelled').count(), 2)
        self.assertEqual(ReviewQueueItem.objects.filter(state__in=['pending', 'open']).count(), 2)

    def test_skipped_topic_retried_next_day_without_reminding_current_question(self):
        Task.objects.filter(pk=self.tasks[0].pk).update(is_active=False)
        second = self.daily().question
        self.assertEqual(second.text, 'Question 1')
        Task.objects.filter(pk=self.tasks[0].pk).update(is_active=True)
        self.daily()
        self.assertFalse(ReviewQueueItem.objects.filter(state='pending').exists())
        tomorrow = NOW + timedelta(days=1)
        self.daily(tomorrow)
        self.answer(second, tomorrow)
        self.assertEqual(continue_review(self.student.pk, now=tomorrow).question.text, 'Question 0')

    def test_settings_change_live_and_never_reissues_processed_date(self):
        DailySchedule.objects.filter(pk=1).update(delivery_time=time(19))
        self.assertEqual(self.daily().status, 'idle')
        DailySchedule.objects.filter(pk=1).update(delivery_time=time(17))
        self.assertIsNotNone(self.daily().question)
        DailySchedule.objects.filter(pk=1).update(delivery_time=time(20))
        self.assertEqual(self.daily(NOW + timedelta(hours=3)).status, 'idle')
        self.assertEqual(DailyReviewRun.objects.count(), 1)

    def test_batch_and_queue_rollback_together(self):
        with patch('modules.study_sessions.services._advance', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                self.daily()
        self.assertFalse(DailyReviewRun.objects.exists())
        self.assertFalse(StudySession.objects.exists())
        self.assertFalse(ReviewQueueItem.objects.exists())
        self.assertIsNotNone(self.daily().question)

    def test_queue_guards_and_original_triggers_survive_migration(self):
        self.daily()
        item = ReviewQueueItem.objects.get(state='pending')
        other = Student.objects.create(course=self.course, telegram_id=43, first_name='Other')
        other_session = StudySession.objects.create(student=other, started_at=NOW)
        for changes in [{'session_id': other_session.pk}, {'state': 'open'}, {'state': 'invalid'}]:
            with self.assertRaises(IntegrityError), transaction.atomic():
                ReviewQueueItem.objects.filter(pk=item.pk).update(**changes)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReviewQueueItem.objects.create(session=item.session, assignment=item.assignment, enqueued_at=NOW)
        with connection.cursor() as cursor:
            names = {row[0] for row in cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertTrue({'attempt_insert_relationships', 'attempt_update_relationships',
                         'session_update_relationships', 'assignment_history_relationships',
                         'queue_insert_relationships', 'queue_update_relationships',
                         'queue_session_relationships', 'queue_assignment_relationships'} <= names)

    def test_admin_singleton_permissions_and_time_validation(self):
        url = reverse('admin:study_sessions_dailyschedule_change', args=[1])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(get_user_model().objects.create_superuser('teacher', password='test'))
        self.assertEqual(self.client.post(url, {'delivery_time': '19:15'}).status_code, 302)
        self.assertEqual(DailySchedule.objects.get().delivery_time, time(19, 15))
        self.assertEqual(self.client.post(url, {'delivery_time': '25:00'}).status_code, 200)
        self.assertEqual(DailySchedule.objects.get().delivery_time, time(19, 15))
        self.assertEqual(self.client.get(reverse('admin:study_sessions_dailyschedule_add')).status_code, 403)
        self.assertEqual(self.client.post(reverse('admin:study_sessions_dailyschedule_delete', args=[1]),
                                         {'post': 'yes'}).status_code, 403)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DailySchedule.objects.create(pk=2)


class SchedulerDeliveryTests(DailyFixture, TransactionTestCase):
    async def tick(self, now=NOW, *, fail_chat=None):
        outgoing = []
        bot = Bot('123456789:' + 'a' * 35)
        async def capture(_bot, method, **kwargs):
            outgoing.append(method)
            if method.chat_id == fail_chat:
                raise OSError('delivery failed secret-reference')
            if getattr(method, 'document', None):
                self.assertTrue(b''.join([part async for part in method.document.read(bot)]))
            return Message.model_validate({'message_id': len(outgoing), 'date': 0,
                                           'chat': {'id': method.chat_id, 'type': 'private'}})
        try:
            with patch.object(bot.session, 'make_request', side_effect=capture):
                await run_tick(bot, now=now)
        finally:
            await bot.session.close()
        self.assertTrue(all(method.parse_mode is None for method in outgoing))
        self.assertNotIn('secret-reference', str([m.model_dump() for m in outgoing]))
        return outgoing

    async def test_files_in_order_and_no_reminder_after_restart(self):
        for name in ['first.txt', 'second.txt']:
            await sync_to_async(TaskAttachment.objects.create)(task=self.tasks[0], file=SimpleUploadedFile(name, b'file'))
        outgoing = await self.tick()
        self.assertEqual([m.__api_method__ for m in outgoing], ['sendMessage', 'sendMessage', 'sendDocument', 'sendDocument'])
        self.assertEqual([m.document.filename for m in outgoing[2:]], ['first.txt', 'second.txt'])
        self.assertEqual(await self.tick(), [])
        self.assertEqual(await self.tick(NOW + timedelta(days=1)), [])
        current = await sync_to_async(current_question)(self.student.pk)
        manual = await sync_to_async(start_review)(self.student.pk, now=NOW + timedelta(days=1))
        self.assertEqual(manual.question, current)

    async def test_failure_does_not_stop_other_student_and_manual_recovers(self):
        other = await sync_to_async(Student.objects.create)(course=self.course, telegram_id=43, first_name='Other')
        await sync_to_async(assign_topics)(other.pk, [self.topics[0].pk], now=NOW)
        with self.assertLogs('modules.delivery.outbox', level='ERROR') as logs:
            outgoing = await self.tick(fail_chat=42)
        self.assertNotIn('secret-reference', '\n'.join(logs.output))
        self.assertEqual([m.chat_id for m in outgoing], [42, 43, 43])
        original = await sync_to_async(current_question)(self.student.pk)
        self.assertIsNotNone(original)
        self.assertEqual(await self.tick(), [])
        self.assertEqual((await sync_to_async(start_review)(self.student.pk, now=NOW)).question, original)

    async def test_empty_topics_are_silent_and_marked_once(self):
        await sync_to_async(Task.objects.update)(is_active=False)
        self.assertEqual(await self.tick(), [])
        self.assertEqual(await self.tick(), [])
        self.assertEqual(await sync_to_async(DailyReviewRun.objects.count)(), 1)
        self.assertEqual(await sync_to_async(ReviewQueueItem.objects.count)(), 2)


class SchedulerCommandTests(TestCase):
    def test_missing_token(self):
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': ''}), self.assertRaisesMessage(CommandError, 'TELEGRAM_BOT_TOKEN'):
            call_command('run_scheduler', skip_checks=True, stdout=io.StringIO())

    async def test_loop_ticks_immediately_and_sleeps_2_seconds(self):
        with patch('modules.delivery.scheduler.run_tick', new_callable=AsyncMock) as tick, patch(
                'modules.delivery.scheduler.asyncio.sleep', new_callable=AsyncMock,
                side_effect=asyncio.CancelledError) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await run_scheduler(object())
            tick.assert_awaited_once()
            sleep.assert_awaited_once_with(2)

    async def test_once_and_network_session_cleanup(self):
        from modules.study_sessions.management.commands.run_scheduler import run
        for failure in [None, RuntimeError('failure'), asyncio.CancelledError()]:
            with patch('modules.study_sessions.management.commands.run_scheduler.run_scheduler',
                       new_callable=AsyncMock, side_effect=failure) as scheduler:
                if failure is None:
                    await run('123456789:' + 'a' * 35, once=True)
                else:
                    with self.assertRaises(type(failure)):
                        await run('123456789:' + 'a' * 35, once=True)
                self.assertTrue(scheduler.call_args.kwargs['once'])
                self.assertTrue(scheduler.call_args.args[0].session._session.closed)
