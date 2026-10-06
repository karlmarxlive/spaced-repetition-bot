from datetime import timedelta
from pathlib import Path
import os
import asyncio
import subprocess
import sys
from unittest.mock import patch

from aiogram import Bot
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
                                TelegramRetryAfter, TelegramServerError, TelegramUnauthorizedError)
from aiogram.methods import SendMessage
from aiogram.types import Message
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from bot.texts import REPLIES, START_TEXT
from modules.delivery.application import daily_review, process_event
from modules.delivery.models import IncomingEvent, OutgoingMessage, QuestionDelivery
from modules.delivery.outbox import claim, classify, drain, finish, MAX_ATTEMPTS
from modules.materials.models import Course, Task, TaskAttachment, Topic
from modules.repetitions.models import TopicProgress
from modules.study_sessions.models import Attempt, DailyReviewRun, DailySchedule, StudySession
from modules.study_sessions.services import start_review
from modules.users.models import Invitation, Student
from modules.users.services import assign_topics
from tests.test_daily_reviews import NOW

BOT_ID = 123456789


def event(update_id, kind='answer', text='yes', *, message_id=None, reply_id=None, user_id=42):
    return dict(bot_id=BOT_ID, update_id=update_id, chat_id=user_id, chat_type='private',
                message_id=message_id if message_id is not None else update_id * 100,
                kind=kind, text=text, reply_id=reply_id,
                user={'id': user_id, 'first_name': 'Student', 'is_bot': False})


class LessonFixture:
    def setUp(self):
        DailySchedule.objects.update_or_create(pk=1, defaults={"delivery_time": "18:00"})
        self.course = Course.objects.create(title='Course')
        self.student = Student.objects.create(course=self.course, telegram_id=42, first_name='Student')
        self.topics = [Topic.objects.create(course=self.course, title=f'Topic {i}', order=i) for i in range(2)]
        self.tasks = [Task.objects.create(topic=t, title=t.title, question=f'Question {i}', answer='yes', is_active=True)
                      for i, t in enumerate(self.topics)]
        assign_topics(self.student.pk, [t.pk for t in self.topics], now=NOW)

    def process(self, incoming, now=NOW):
        return process_event(incoming, now=now)

    def confirm(self, *, start_id=110, now=NOW):
        while row := claim(bot_id=BOT_ID, now=now):
            self.assertTrue(finish(row, now=now, message_id=start_id))
            start_id += 1
        return start_id - 1


class AtomicCycleTests(LessonFixture, TestCase):
    def failed_start(self):
        self.process(event(1, 'start', user_id=43))
        row = claim(bot_id=BOT_ID, now=NOW)
        self.assertTrue(finish(row, now=NOW, error='chat_unavailable'))
        return row

    def test_duplicate_start_does_not_reset_failure_or_enqueue(self):
        self.failed_start()
        incoming = event(2, 'start', user_id=43)
        self.process(incoming)
        row = claim(bot_id=BOT_ID, now=NOW)
        finish(row, now=NOW, error='network', retry_seconds=5)
        before = list(OutgoingMessage.objects.values())
        self.assertFalse(self.process(incoming, now=NOW + timedelta(seconds=1)))
        self.assertEqual(list(OutgoingMessage.objects.values()), before)
        row = claim(bot_id=BOT_ID, now=NOW + timedelta(seconds=5))
        finish(row, now=NOW + timedelta(seconds=5), error='chat_unavailable')
        before = list(OutgoingMessage.objects.values())
        self.assertFalse(self.process(incoming, now=NOW + timedelta(seconds=10)))
        self.assertEqual(list(OutgoingMessage.objects.values()), before)
        self.assertEqual(IncomingEvent.objects.count(), 2)

    def test_start_recovery_registration_and_event_roll_back_together(self):
        self.failed_start()
        before = list(OutgoingMessage.objects.values())
        invitation = Invitation.objects.create(course=self.course)
        incoming = event(2, 'start', user_id=43)
        incoming['payload'] = invitation.token
        with patch('modules.delivery.application.enqueue_text', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.process(incoming)
        self.assertEqual(list(OutgoingMessage.objects.values()), before)
        self.assertFalse(IncomingEvent.objects.filter(update_id=2).exists())
        self.assertFalse(Student.objects.filter(telegram_id=43).exists())
        invitation.refresh_from_db()
        self.assertIsNone(invitation.used_at)
        self.process(incoming)
        self.assertEqual(OutgoingMessage.objects.first().state, 'pending')
        self.assertEqual(IncomingEvent.objects.get(update_id=2).student_id,
                         Student.objects.get(telegram_id=43).pk)

    def test_start_from_group_bot_missing_or_invalid_user_does_not_resume(self):
        row = self.failed_start()
        for index, changes in enumerate([
            {'chat_type': 'group'},
            {'user': {'id': 43, 'first_name': 'Bot', 'is_bot': True}},
            {'user': None},
            {'user': {'id': 43, 'first_name': '', 'is_bot': False}},
        ], 2):
            with self.subTest(changes=changes):
                self.process({**event(index, 'start', user_id=43), **changes})
                row.refresh_from_db()
                self.assertEqual((row.state, row.attempts), ('failed', 1))

    def test_duplicate_update_after_next_question_and_restart_is_noop(self):
        self.process(event(1, 'review'))
        self.confirm()
        self.process(event(2))
        first = Attempt.objects.get(status='correct')
        second = Attempt.objects.get(status='open')
        count = OutgoingMessage.objects.count()
        self.assertFalse(self.process(event(2)))
        self.confirm(start_id=210)
        self.assertFalse(self.process(event(2)))
        second.refresh_from_db()
        self.assertEqual(second.status, 'open')
        self.assertEqual(OutgoingMessage.objects.count(), count)
        self.assertEqual(IncomingEvent.objects.get(update_id=2).attempt_id, first.pk)
        self.assertEqual(TopicProgress.objects.get(assignment=first.assignment).interval_step, 1)
        # Identical text on a different delivered task is legitimate.
        self.process(event(3))
        self.assertEqual(Attempt.objects.filter(status='correct').count(), 2)

    def test_two_quick_texts_cannot_answer_undelivered_question(self):
        self.process(event(1, 'review'))
        self.confirm()
        self.process(event(2, message_id=200))
        self.process(event(3, message_id=201))
        self.assertEqual(Attempt.objects.filter(status='correct').count(), 1)
        # Even if worker delivers between processing the two queued messages,
        # their Telegram message IDs still precede the new question's IDs.
        self.confirm(start_id=202)
        self.process(event(4, message_id=201))
        self.assertEqual(Attempt.objects.filter(status='correct').count(), 1)
        self.process(event(5, message_id=500))
        self.assertEqual(Attempt.objects.filter(status='correct').count(), 2)

    def test_question_without_delivery_record_requires_review(self):
        question = start_review(self.student.pk, now=NOW).question
        self.process(event(1, message_id=100))
        self.assertEqual(Attempt.objects.get(pk=question.attempt_id).status, 'open')
        self.process(event(2, 'review'))
        last_id = self.confirm()
        self.process(event(3, message_id=last_id + 1))
        self.assertEqual(Attempt.objects.get(pk=question.attempt_id).status, 'correct')

    def test_reply_to_old_unknown_foreign_and_partial_question_is_rejected(self):
        self.process(event(1, 'review'))
        first = Attempt.objects.get()
        first_id = self.confirm()
        self.process(event(2, reply_id=first_id))
        self.confirm(start_id=210)
        for index, reply_id in enumerate([first_id, 999999], 3):
            self.process(event(index, reply_id=reply_id))
        self.assertEqual(Attempt.objects.filter(status='correct').count(), 1)
        foreign = event(8, reply_id=first_id, user_id=43)
        self.process(foreign)
        self.assertEqual(Attempt.objects.get(pk=first.pk).status, 'correct')

    def test_unconfirmed_parts_and_resend_do_not_accept_answers(self):
        self.process(event(1, 'review'))
        row = claim(bot_id=BOT_ID, now=NOW)
        finish(row, now=NOW, message_id=110)
        self.process(event(2, reply_id=110))
        self.assertEqual(Attempt.objects.get().status, 'open')
        before = OutgoingMessage.objects.filter(is_question=True).count()
        self.process(event(3, 'review'))
        self.assertEqual(OutgoingMessage.objects.filter(is_question=True).count(), before)
        self.confirm()
        self.process(event(4, 'review'))
        self.assertEqual(OutgoingMessage.objects.filter(is_question=True).count(), before * 2)
        self.assertEqual(Attempt.objects.count(), 1)

    def test_failure_after_answer_before_continuation_rolls_back_everything(self):
        self.process(event(1, 'review'))
        self.confirm()
        before = OutgoingMessage.objects.count()
        with patch('modules.delivery.application.continue_review', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.process(event(2))
        self.assertFalse(IncomingEvent.objects.filter(update_id=2).exists())
        self.assertEqual(Attempt.objects.get().status, 'open')
        self.assertEqual(TopicProgress.objects.filter(interval_step=0).count(), 2)
        self.assertEqual(OutgoingMessage.objects.count(), before)
        self.process(event(2))
        self.assertEqual(Attempt.objects.filter(status='open').count(), 1)
        self.assertEqual(Attempt.objects.count(), 2)
        self.assertEqual(OutgoingMessage.objects.filter(operation=f'update:{BOT_ID}:2').first().text, 'Верно.')

    def test_failure_after_daily_mark_rolls_back_and_commit_leaves_recoverable_work(self):
        with patch('modules.delivery.application.enqueue_review', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                daily_review(self.student.pk, bot_id=BOT_ID, now=NOW)
        self.assertFalse(DailyReviewRun.objects.exists())
        self.assertFalse(Attempt.objects.exists())
        self.assertTrue(daily_review(self.student.pk, bot_id=BOT_ID, now=NOW))
        count = OutgoingMessage.objects.count()
        self.assertFalse(daily_review(self.student.pk, bot_id=BOT_ID, now=NOW))
        self.assertEqual(OutgoingMessage.objects.count(), count)
        self.confirm()
        self.assertEqual(QuestionDelivery.objects.get().state, 'delivered')

    def test_start_and_information_use_atomic_outbox_and_deduplication(self):
        invitation = Invitation.objects.create(course=self.course)
        incoming = event(1, 'start', user_id=43)
        incoming['payload'] = invitation.token
        with patch('modules.delivery.application.enqueue_text', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.process(incoming)
        self.assertFalse(Student.objects.filter(telegram_id=43).exists())
        invitation.refresh_from_db()
        self.assertIsNone(invitation.used_at)
        self.process(incoming)
        self.assertFalse(self.process(incoming))
        self.assertEqual(OutgoingMessage.objects.count(), 1)
        self.assertEqual(Student.objects.filter(telegram_id=43).count(), 1)
        self.process(event(2, 'unsupported'))
        self.assertIn('без вложений', OutgoingMessage.objects.last().text)

    def test_cancel_disable_reenable_does_not_revive_old_parts(self):
        self.process(event(1, 'review'))
        old = Attempt.objects.get()
        row = claim(bot_id=BOT_ID, now=NOW)
        assign_topics(self.student.pk, [self.topics[1].pk], now=NOW)
        # Network might have delivered this, but late ownership acknowledgement fails.
        self.assertFalse(finish(row, now=NOW, message_id=110))
        self.process(event(2))
        self.assertEqual(Attempt.objects.filter(status='correct').count(), 0)
        self.assertFalse(OutgoingMessage.objects.filter(attempt=old, is_question=True).exclude(state='cancelled').exists())
        assign_topics(self.student.pk, [t.pk for t in self.topics], now=NOW)
        self.process(event(3, 'review'))
        self.confirm(start_id=310)
        self.process(event(4))
        self.confirm(start_id=410)
        self.assertEqual(Attempt.objects.get(pk=old.pk).status, 'cancelled')
        self.process(event(5, 'review'))
        self.assertEqual(Attempt.objects.get(status='open').assignment.topic_id, self.topics[0].pk)

    def test_expired_lease_and_stale_owner_cannot_acknowledge(self):
        self.process(event(1, 'review'))
        first = claim(bot_id=BOT_ID, now=NOW)
        self.assertIsNone(claim(bot_id=BOT_ID, now=NOW))
        later = NOW + timedelta(seconds=61)
        second = claim(bot_id=BOT_ID, now=later)
        self.assertEqual(first.pk, second.pk)
        self.assertNotEqual(first.lease_token, second.lease_token)
        self.assertFalse(finish(first, now=later, message_id=10))
        self.assertTrue(finish(second, now=later, message_id=11))
        self.assertEqual(OutgoingMessage.objects.get(pk=first.pk).attempts, 2)

    def test_error_classification_retry_limit_and_manual_recovery(self):
        method = SendMessage(chat_id=42, text='test')
        cases = [(OSError('secret'), 'network', 5), (TimeoutError(), 'network', 5),
                 (TelegramNetworkError(method=method, message='secret'), 'network', 5),
                 (TelegramServerError(method=method, message='secret'), 'network', 5),
                 (TelegramRetryAfter(method=method, message='secret', retry_after=42), 'rate_limit', 42),
                 (TelegramForbiddenError(method=method, message='secret'), 'chat_unavailable', None),
                 (TelegramBadRequest(method=method, message='secret'), 'telegram_request', None),
                 (TelegramUnauthorizedError(method=method, message='secret'), 'configuration', None),
                 (FileNotFoundError('secret'), 'attachment', None)]
        for error, category, delay in cases:
            self.assertEqual(classify(error, 1), (category, delay))
        self.assertEqual(classify(OSError(), 9), ('network', 900))
        self.process(event(1, 'review'))
        row = claim(bot_id=BOT_ID, now=NOW)
        row.attempts = MAX_ATTEMPTS
        finish(row, now=NOW, error='network', retry_seconds=900)
        self.assertEqual(OutgoingMessage.objects.get(pk=row.pk).state, 'failed')
        self.process(event(2, 'review'))
        self.assertEqual(OutgoingMessage.objects.get(pk=row.pk).state, 'pending')
        self.confirm()
        self.assertEqual(Attempt.objects.count(), 1)
        self.assertEqual(QuestionDelivery.objects.get().state, 'delivered')


class WorkerTests(LessonFixture, TransactionTestCase):
    async def send(self, *, fail_at=None, now=NOW, cancel=False, permanent=False):
        bot = Bot(str(BOT_ID) + ':' + 'a' * 35)
        sent = []
        async def transport(_bot, method, **kwargs):
            self.assertFalse(await sync_to_async(lambda: connection.in_atomic_block)())
            sent.append(method)
            self.assertNotIn('reference', str(method.model_dump()))
            if fail_at and len(sent) == fail_at:
                if permanent:
                    raise TelegramForbiddenError(method=method, message='private')
                raise OSError('token url reference')
            if cancel:
                await sync_to_async(assign_topics)(self.student.pk, [], now=now)
            return Message.model_validate({'message_id': 1000 + len(sent), 'date': 0,
                                          'chat': {'id': method.chat_id, 'type': 'private'}})
        try:
            with patch.object(bot.session, 'make_request', side_effect=transport):
                await drain(bot, now=now)
        finally:
            await bot.session.close()
        return sent

    async def check_start_recovery(self, *, user_id=43, invite=False):
        progress = await sync_to_async(list)(TopicProgress.objects.values())
        await sync_to_async(self.process)(event(1, 'start', user_id=user_id))
        await self.send(fail_at=1, permanent=True)
        failed = await sync_to_async(OutgoingMessage.objects.get)()
        self.assertEqual((failed.state, failed.attempts), ('failed', 1))
        self.assertEqual(await self.send(), [])
        incoming = event(2, 'start', user_id=user_id)
        if invite:
            invitation = await sync_to_async(Invitation.objects.create)(course=self.course)
            incoming['payload'] = invitation.token
        later = NOW + timedelta(seconds=10)
        await sync_to_async(self.process)(incoming, now=later)
        await sync_to_async(failed.refresh_from_db)()
        self.assertEqual((failed.state, failed.attempts, failed.error, failed.next_attempt_at),
                         ('pending', 0, '', later))
        sent = await self.send(now=later)
        expected = REPLIES['registered'] if invite else REPLIES['existing'] if user_id == 42 else START_TEXT
        self.assertEqual([m.text for m in sent], [failed.text, expected])
        self.assertEqual(await sync_to_async(OutgoingMessage.objects.filter(state='sent').count)(), 2)
        self.assertEqual(await sync_to_async(Student.objects.filter(telegram_id=user_id).exists)(),
                         invite or user_id == 42)
        if invite:
            await sync_to_async(invitation.refresh_from_db)()
            self.assertIsNotNone(invitation.used_at)
            registered = await sync_to_async(Student.objects.get)(telegram_id=user_id)
            self.assertEqual(invitation.student_id, registered.pk)
        self.assertFalse(await sync_to_async(Attempt.objects.exists)())
        self.assertFalse(await sync_to_async(StudySession.objects.exists)())
        self.assertEqual(await sync_to_async(list)(TopicProgress.objects.values()), progress)

    async def test_start_without_invitation_resumes_failed_queue(self):
        await self.check_start_recovery()

    async def test_start_with_invitation_registers_and_delivers_welcome_after_failure(self):
        await self.check_start_recovery(invite=True)

    async def test_start_registered_student_resumes_without_opening_lesson(self):
        await self.check_start_recovery(user_id=42)

    async def test_start_resumes_only_unconfirmed_question_parts(self):
        await sync_to_async(self.process)(event(1, 'review'))
        await self.send(fail_at=2, permanent=True)
        confirmed = await sync_to_async(OutgoingMessage.objects.get)(state='sent')
        progress = await sync_to_async(list)(TopicProgress.objects.values())
        attempts = await sync_to_async(list)(Attempt.objects.values())
        await sync_to_async(self.process)(event(2, 'start'))
        sent = await self.send()
        self.assertEqual([m.text for m in sent], ['Question 0', REPLIES['existing']])
        self.assertEqual((await sync_to_async(OutgoingMessage.objects.get)(pk=confirmed.pk)).attempts, 1)
        self.assertEqual((await sync_to_async(QuestionDelivery.objects.get)()).state, 'delivered')
        self.assertEqual(await sync_to_async(list)(Attempt.objects.values()), attempts)
        self.assertEqual(await sync_to_async(list)(TopicProgress.objects.values()), progress)

    async def test_start_does_not_revive_cancelled_question(self):
        await sync_to_async(self.process)(event(1, 'review'))
        await self.send(fail_at=1, permanent=True)
        await sync_to_async(assign_topics)(self.student.pk, [], now=NOW)
        attempts = await sync_to_async(list)(Attempt.objects.values())
        await sync_to_async(self.process)(event(2, 'start'))
        sent = await self.send()
        self.assertEqual([m.text for m in sent], [REPLIES['existing']])
        self.assertFalse(await sync_to_async(OutgoingMessage.objects.filter(is_question=True).exclude(state='cancelled').exists)())
        self.assertEqual(await sync_to_async(list)(Attempt.objects.values()), attempts)

    async def test_partial_long_question_files_resume_only_failed_part(self):
        text = '😀x' * 3000
        await sync_to_async(Task.objects.filter(pk=self.tasks[0].pk).update)(question=text, answer='private-reference')
        for content in [b'one', b'two']:
            await sync_to_async(TaskAttachment.objects.create)(task=self.tasks[0], file=SimpleUploadedFile('file.txt', content))
        await sync_to_async(self.process)(event(1, 'review'))
        sent = await self.send(fail_at=6)
        self.assertEqual(len(sent), 6)
        self.assertEqual(''.join(m.text for m in sent[1:] if getattr(m, 'text', None)), text)
        self.assertEqual((await sync_to_async(QuestionDelivery.objects.get)()).state, 'pending')
        again = await self.send(now=NOW + timedelta(seconds=5))
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0].__api_method__, 'sendDocument')
        self.assertEqual((await sync_to_async(QuestionDelivery.objects.get)()).state, 'delivered')

    async def test_broken_chat_does_not_block_other_student_and_before_daily_time(self):
        from modules.delivery.scheduler import run_tick
        await sync_to_async(Student.objects.create)(course=self.course, telegram_id=43, first_name='Other')
        await sync_to_async(self.process)(event(1, 'review'))
        await sync_to_async(self.process)(event(2, 'unsupported', user_id=43))
        bot = Bot(str(BOT_ID) + ':' + 'a' * 35)
        chats = []
        async def transport(_bot, method, **kwargs):
            if method.chat_id == 42:
                raise TelegramForbiddenError(method=method, message='private')
            chats.append(method.chat_id)
            return Message.model_validate({'message_id': 500, 'date': 0, 'chat': {'id': method.chat_id, 'type': 'private'}})
        try:
            with patch.object(bot.session, 'make_request', side_effect=transport):
                await run_tick(bot, now=NOW.replace(hour=1))
                # Pending messages use formation time, advance without daily formation.
                await drain(bot, now=NOW)
        finally:
            await bot.session.close()
        self.assertEqual(chats, [43])
        self.assertEqual((await sync_to_async(OutgoingMessage.objects.get)(state='failed')).error, 'chat_unavailable')
        await sync_to_async(self.process)(event(3, 'review'))
        await self.send()
        self.assertEqual((await sync_to_async(QuestionDelivery.objects.get)()).state, 'delivered')

    async def test_cancel_while_in_network_cannot_revive_or_accept(self):
        await sync_to_async(self.process)(event(1, 'review'))
        sent = await self.send(cancel=True)
        self.assertEqual(len(sent), 1)
        await sync_to_async(self.process)(event(2, reply_id=1001))
        self.assertEqual(await sync_to_async(Attempt.objects.filter(status='correct').count)(), 0)
        self.assertEqual((await sync_to_async(Attempt.objects.get)()).status, 'cancelled')

    async def test_crash_after_telegram_acceptance_before_ack_only_repeats_that_part(self):
        await sync_to_async(self.process)(event(1, 'review'))
        with patch('modules.delivery.outbox.finish', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                await self.send()
        self.assertEqual(await sync_to_async(OutgoingMessage.objects.filter(state='sending').count)(), 1)
        sent = await self.send(now=NOW + timedelta(seconds=61))
        self.assertEqual(len(sent), 2)
        self.assertEqual(await sync_to_async(Attempt.objects.count)(), 1)
        self.assertEqual((await sync_to_async(QuestionDelivery.objects.get)()).state, 'delivered')

    async def test_retry_after_and_timeout_do_not_sleep_or_block_other_chat(self):
        await sync_to_async(self.process)(event(1, 'review'))
        await sync_to_async(Student.objects.create)(course=self.course, telegram_id=43, first_name='Other')
        await sync_to_async(self.process)(event(2, 'unsupported', user_id=43))
        bot = Bot(str(BOT_ID) + ':' + 'a' * 35)
        sent = []
        async def transport(_bot, method, **kwargs):
            sent.append(method.chat_id)
            if method.chat_id == 42:
                raise TelegramRetryAfter(method=method, message='secret', retry_after=42)
            return Message.model_validate({'message_id': 500, 'date': 0, 'chat': {'id': method.chat_id, 'type': 'private'}})
        try:
            with patch.object(bot.session, 'make_request', side_effect=transport):
                await drain(bot, now=NOW)
            self.assertEqual(sent, [42, 43])
            failed = await sync_to_async(OutgoingMessage.objects.get)(state='retry')
            self.assertEqual(failed.next_attempt_at, NOW + timedelta(seconds=42))
            self.assertEqual(failed.error, 'rate_limit')
            sent.clear()
            async def timeout(_bot, method, **kwargs):
                sent.append(method.chat_id)
                raise TimeoutError('secret')
            with patch.object(bot.session, 'make_request', side_effect=timeout):
                await drain(bot, now=NOW + timedelta(seconds=41))
                self.assertEqual(sent, [])
                await drain(bot, now=NOW + timedelta(seconds=42))
            failed = await sync_to_async(OutgoingMessage.objects.get)(state='retry')
            self.assertEqual(failed.attempts, 2)
            self.assertEqual(failed.next_attempt_at, NOW + timedelta(seconds=52))
            self.assertEqual(failed.error, 'network')
        finally:
            await bot.session.close()

    async def test_configuration_error_stops_worker_without_touching_progress(self):
        await sync_to_async(self.process)(event(1, 'review'))
        bot = Bot(str(BOT_ID) + ':' + 'a' * 35)
        async def transport(_bot, method, **kwargs):
            raise TelegramUnauthorizedError(method=method, message='secret')
        try:
            with patch.object(bot.session, 'make_request', side_effect=transport):
                with self.assertRaises(TelegramUnauthorizedError):
                    await drain(bot, now=NOW)
        finally:
            await bot.session.close()
        self.assertEqual((await sync_to_async(OutgoingMessage.objects.get)(state='failed')).error, 'configuration')
        self.assertEqual(await sync_to_async(TopicProgress.objects.filter(interval_step=0).count)(), 2)


class StatisticsTests(LessonFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.teacher = get_user_model().objects.create_superuser('teacher', password='test')
        self.client.force_login(self.teacher)
        self.url = reverse('admin:users_student_statistics', args=[self.student.pk])

    def test_student_scope_snapshot_filters_counts_and_readonly(self):
        self.process(event(1, 'review'))
        self.confirm()
        self.process(event(2))
        attempt = Attempt.objects.get(status='correct')
        self.tasks[0].question = 'Changed bank'
        self.tasks[0].answer = 'Changed reference'
        self.tasks[0].topic = self.topics[1]
        self.tasks[0].save()
        self.topics[0].title = 'Renamed'
        self.topics[0].save()
        other = Student.objects.create(course=self.course, telegram_id=43, first_name='Other')
        assign_topics(other.pk, [self.topics[1].pk], now=NOW)
        self.process(event(3, 'review', user_id=43))
        response = self.client.get(self.url, {'student': other.pk, 'topic': self.topics[0].pk})
        self.assertEqual(response.context['counts'], {'correct': 1})
        self.assertEqual(list(response.context['history']), [attempt])
        self.assertEqual(response.context['current'].student_id, self.student.pk)
        detail = reverse('admin:users_student_attempt', args=[self.student.pk, attempt.pk])
        self.assertContains(self.client.get(detail), 'Question 0')
        self.assertNotContains(self.client.get(detail), 'Changed bank')
        self.assertEqual(self.client.post(detail, {'reference': 'forged'}).status_code, 405)
        self.assertEqual(self.client.post(self.url, {'action': 'delete_selected'}).status_code, 405)
        foreign_attempt = Attempt.objects.get(student=other)
        self.assertEqual(self.client.get(reverse('admin:users_student_attempt', args=[self.student.pk, foreign_attempt.pk])).status_code, 404)
        self.assertEqual(self.client.get(self.url, {'status': 'open'}).context['counts'], {'open': 1})
        self.assertEqual(self.client.get(self.url, {'from': '2099-01-01'}).context['counts'], {})

    def test_escaped_snapshots_permissions_and_protected_files(self):
        self.tasks[0].question = '<script>alert(1)</script>'
        self.tasks[0].answer = '<b>secret</b>'
        self.tasks[0].save()
        attachment = TaskAttachment.objects.create(task=self.tasks[0], file=SimpleUploadedFile('one.txt', b'one'))
        self.process(event(1, 'review'))
        attempt = Attempt.objects.get()
        detail = reverse('admin:users_student_attempt', args=[self.student.pk, attempt.pk])
        file_url = reverse('admin:users_student_attempt_file', args=[self.student.pk, attempt.pk, 0])
        self.assertContains(self.client.get(detail), '&lt;script&gt;')
        self.assertNotContains(self.client.get(detail), '<b>secret</b>')
        response = self.client.get(file_url)
        self.assertEqual(b''.join(response.streaming_content), b'one')
        self.assertEqual(self.client.get('/media/' + attachment.file.name).status_code, 404)
        staff = get_user_model().objects.create_user('staff', is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename='view_student'))
        self.client.force_login(staff)
        for url in (self.url, detail, file_url):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_pagination_and_disabled_progress_are_explicit(self):
        self.process(event(1, 'review'))
        assign_topics(self.student.pk, [], now=NOW)
        response = self.client.get(self.url)
        self.assertEqual(response.context['counts'], {'cancelled': 1})
        self.assertContains(response, 'Ступень 0')
        self.assertContains(response, 'выдача отключена')
        self.assertContains(response, 'Europe/Moscow')

    def test_history_paginates_without_per_row_queries(self):
        from django.test.utils import CaptureQueriesContext
        assignment = self.student.topic_assignments.get(topic=self.topics[0])
        for index in range(30):
            session = StudySession.objects.create(student=self.student, started_at=NOW,
                finished_at=NOW, state='finished')
            Attempt.objects.create(student=self.student, session=session, assignment=assignment,
                task=self.tasks[0], opened_at=NOW, finished_at=NOW, status='incorrect',
                question=f'Question {index}', reference='yes', response='<img src=x onerror=alert(1)>',
                topic_title='Original', attachments=[], task_order=0)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url, {'page': '2'})
        self.assertEqual(len(response.context['history']), 5)
        self.assertEqual(response.context['counts'], {'incorrect': 30})
        self.assertLess(len(queries), 20)
        detail = reverse('admin:users_student_attempt', args=[self.student.pk, Attempt.objects.first().pk])
        self.assertContains(self.client.get(detail), '&lt;img')


class FileReliabilityTests(TestCase):
    def test_independent_sqlite_connections_and_process_restart(self):
        result = subprocess.run([sys.executable, '-m', 'tests.reliability_demo'], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Stage 7 SQLite recovery: OK', result.stdout)


class PollingGuaranteeTests(TransactionTestCase):
    async def test_retry_finishes_before_generator_advances_offset(self):
        from bot.polling import ReliableDispatcher
        from aiogram.types import Update
        from unittest.mock import AsyncMock
        calls = []
        class FakeBot:
            id = BOT_ID
            session = type('Session', (), {'timeout': 15})()
            async def __call__(self, method, **kwargs):
                calls.append(method.offset)
                if len(calls) == 1:
                    return [Update(update_id=17)]
                raise asyncio.CancelledError
        bot = FakeBot()
        dispatcher = ReliableDispatcher()
        updates = dispatcher._listen_updates(bot)
        update = await anext(updates)
        self.assertEqual(calls, [None])
        processed = []
        async def feed(*args, **kwargs):
            self.assertEqual(calls, [None])
            processed.append(update.update_id)
            if len(processed) == 1:
                raise RuntimeError('private payload')
        try:
            with patch.object(dispatcher, 'feed_update', side_effect=feed), patch('bot.polling.asyncio.sleep', new_callable=AsyncMock) as sleep:
                with self.assertLogs('bot', level='ERROR') as logs:
                    await dispatcher._process_update(bot, update)
                self.assertNotIn('private payload', '\n'.join(logs.output))
                sleep.assert_awaited_once_with(5)
            # Request the next batch: offset only advances after successful processing.
            with self.assertRaises(asyncio.CancelledError):
                await anext(updates)
            self.assertEqual(calls, [None, 18])
            self.assertEqual(processed, [17, 17])
        finally:
            await updates.aclose()
