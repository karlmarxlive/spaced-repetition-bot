from pathlib import Path
from datetime import timedelta
from unittest.mock import patch

from aiogram import Bot, Dispatcher
from aiogram.types import Message, Update
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TransactionTestCase
from django.db import connection

from bot.handlers import create_router
from modules.delivery.scheduler import run_tick
from modules.delivery.services import text_parts
from modules.materials.models import Course, Task, TaskAttachment, Topic
from modules.repetitions.models import TopicProgress
from modules.repetitions.services import apply_result
from modules.study_sessions.models import Attempt, DailyReviewRun, DailySchedule, ReviewQueueItem, StudySession
from modules.study_sessions.services import current_question
from modules.users.models import Student
from modules.users.services import assign_topics
from tests.test_repetitions import NOW
from tests.test_daily_reviews import NOW as DAILY_NOW


class TextPartsTests(SimpleTestCase):
    def test_lossless_utf16_boundaries(self):
        text = ('<>&_* 😀\n ' * 1700) + 'end'
        parts = list(text_parts(text))
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(0 < len(p.encode('utf-16-le')) // 2 <= 4096 for p in parts))


class ReviewDeliveryTests(TransactionTestCase):
    def setUp(self):
        self.course = Course.objects.create(title='Course')
        self.student = Student.objects.create(course=self.course, telegram_id=42, first_name='Student')
        self.topics = [Topic.objects.create(course=self.course, title=f'Topic {n}', order=n) for n in range(2)]
        self.tasks = [Task.objects.create(topic=topic, title=f'Task {n}', question=f'Question {n}',
                                         answer=f'secret-reference-{n}', is_active=True)
                      for n, topic in enumerate(self.topics)]
        assign_topics(self.student.pk, [t.pk for t in self.topics], now=NOW)

    async def dispatch(self, text=None, *, chat_type='private', user_id=42, extra=None, fail_at=None,
                       now=NOW, tick_during_result=False):
        self.event_id = getattr(self, 'event_id', 0) + 100
        bot = Bot('123456789:' + 'a' * 35)
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router())
        message = {'message_id': self.event_id, 'date': 0, 'chat': {'id': user_id, 'type': chat_type},
                   'from': {'id': user_id, 'first_name': 'Same name', 'is_bot': False}}
        if text is not None:
            message['text'] = text
            if text.startswith('/'):
                message['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])}]
        message.update(extra or {})
        outgoing = []
        async def capture(_bot, method, **kwargs):
            self.assertFalse(await sync_to_async(lambda: connection.in_atomic_block)())
            outgoing.append(method)
            if tick_during_result and getattr(method, 'text', None) in ('Верно.', 'Неверно.'):
                # Answer and continuation committed; result is currently leased in transport.
                self.assertEqual(await run_tick(bot, now=DAILY_NOW), 0)
                self.assertEqual((await sync_to_async(StudySession.objects.get)()).state, 'finished')
            if fail_at and len(outgoing) == fail_at:
                raise OSError('transport failed secret-reference-0')
            if getattr(method, 'document', None):
                # Exercise local file reading too; never call the network.
                data = b''.join([part async for part in method.document.read(bot)])
                self.assertTrue(data)
            return Message.model_validate({'message_id': self.event_id + len(outgoing), 'date': 0,
                                            'chat': {'id': user_id, 'type': chat_type}})
        try:
            with patch.object(bot.session, 'make_request', side_effect=capture), patch(
                    'bot.handlers.timezone.now', return_value=now):
                await dispatcher.feed_update(bot, Update.model_validate({'update_id': self.event_id, 'message': message}))
        finally:
            await bot.session.close()
        for method in outgoing:
            if text != '/start':
                self.assertIsNone(method.parse_mode)
            self.assertNotIn('secret-reference', str(method.model_dump()))
        return outgoing

    def texts(self, outgoing):
        return [m.text for m in outgoing if getattr(m, 'text', None)]

    async def check_last_answer_with_scheduler(self, response, correct):
        await sync_to_async(DailySchedule.objects.update_or_create)(pk=1, defaults={'delivery_time': '18:00'})
        await sync_to_async(assign_topics)(self.student.pk, [self.topics[0].pk], now=DAILY_NOW)
        before = DAILY_NOW - timedelta(seconds=1)
        await self.dispatch('/review', now=before)
        with patch('modules.study_sessions.services.apply_result', wraps=apply_result) as apply:
            outgoing = self.texts(await self.dispatch(response, now=before, tick_during_result=True))
        apply.assert_called_once()
        self.assertEqual(outgoing, ['Верно.' if correct else 'Неверно.',
            f'Занятие завершено. Ответов: 1; верных: {int(correct)}; неверных: {int(not correct)}.'])
        self.assertEqual(await sync_to_async(StudySession.objects.count)(), 1)
        self.assertEqual(await sync_to_async(Attempt.objects.count)(), 1)
        self.assertEqual(await sync_to_async(ReviewQueueItem.objects.count)(), 1)
        self.assertEqual(await sync_to_async(DailyReviewRun.objects.count)(), 1)
        progress = await sync_to_async(TopicProgress.objects.get)(assignment__topic=self.topics[0])
        self.assertEqual(progress.interval_step, int(correct))
        self.assertEqual(progress.next_review_date, DAILY_NOW.date() + timedelta(days=1))
        self.assertIsNone(await sync_to_async(current_question)(self.student.pk))

    async def test_correct_last_answer_with_scheduler_during_result(self):
        await self.check_last_answer_with_scheduler('secret-reference-0', True)

    async def test_incorrect_last_answer_with_scheduler_during_result(self):
        await self.check_last_answer_with_scheduler('wrong', False)

    async def test_accumulated_summary_with_scheduler_during_last_result(self):
        before = DAILY_NOW - timedelta(seconds=1)
        await sync_to_async(DailySchedule.objects.update_or_create)(pk=1, defaults={'delivery_time': '18:00'})
        skipped = await sync_to_async(Topic.objects.create)(course=self.course, title='Skipped', order=2)
        cancelled = await sync_to_async(Topic.objects.create)(course=self.course, title='Cancelled', order=3)
        last = await sync_to_async(Topic.objects.create)(course=self.course, title='Last', order=4)
        for topic in (cancelled, last):
            await sync_to_async(Task.objects.create)(topic=topic, title=topic.title,
                question=topic.title, answer='last-reference', is_active=True)
        await sync_to_async(assign_topics)(self.student.pk,
            [t.pk for t in self.topics] + [skipped.pk, cancelled.pk, last.pk], now=before)
        await self.dispatch('/review', now=before)
        await self.dispatch('secret-reference-0', now=before)
        await self.dispatch('wrong', now=before)  # Skips empty topic, opens cancellable question.
        await sync_to_async(assign_topics)(self.student.pk,
            [t.pk for t in self.topics] + [last.pk], now=before)
        await self.dispatch('late cancelled answer', now=before)
        snapshot = await sync_to_async(lambda: list(TopicProgress.objects.order_by('pk').values()))()
        with patch('modules.study_sessions.services.apply_result', wraps=apply_result) as apply:
            outgoing = self.texts(await self.dispatch('last-reference', now=before, tick_during_result=True))
        apply.assert_called_once()
        self.assertEqual(outgoing, ['Верно.', 'Занятие завершено. Ответов: 3; верных: 2; неверных: 1.'
                                   ' Пропущено тем: 1; отменено: 1.'])
        self.assertEqual(await sync_to_async(StudySession.objects.count)(), 1)
        self.assertEqual(await sync_to_async(Attempt.objects.count)(), 4)
        self.assertEqual(await sync_to_async(ReviewQueueItem.objects.count)(), 5)
        final = await sync_to_async(lambda: list(TopicProgress.objects.order_by('pk').values()))()
        last_progress = await sync_to_async(TopicProgress.objects.get)(assignment__topic=last)
        for old, new in zip(snapshot, final):
            if old['assignment_id'] == last_progress.assignment_id:
                self.assertEqual(new['interval_step'], 1)
                self.assertEqual(new['next_review_date'], DAILY_NOW.date() + timedelta(days=1))
            else:
                self.assertEqual(new, old)

    async def test_complete_lesson_result_precedes_next_question(self):
        self.assertIn('Question 0', self.texts(await self.dispatch('/review')))
        first = await sync_to_async(current_question)(self.student.pk)
        self.assertIn('Question 0', self.texts(await self.dispatch('/review')))
        self.assertEqual(await sync_to_async(current_question)(self.student.pk), first)
        outgoing = self.texts(await self.dispatch('secret-reference-0'))
        self.assertEqual(outgoing[0], 'Верно.')
        self.assertEqual(outgoing[-1], 'Question 1')
        outgoing = self.texts(await self.dispatch('wrong'))
        self.assertEqual(outgoing[0], 'Неверно.')
        self.assertIn('Ответов: 2; верных: 1; неверных: 1', outgoing[-1])
        self.assertIsNone(await sync_to_async(current_question)(self.student.pk))

    async def test_commands_empty_media_and_private_identity_guards(self):
        self.assertIn('/review', self.texts(await self.dispatch('no open question'))[0])
        await self.dispatch('/review')
        first = await sync_to_async(current_question)(self.student.pk)
        for text in ['/unknown', '/start', ' /unknown', ' \t\n']:
            await self.dispatch(text)
        for payload in [
            {'photo': [{'file_id': 'p', 'file_unique_id': 'p', 'width': 1, 'height': 1}], 'caption': 'wrong'},
            {'document': {'file_id': 'd', 'file_unique_id': 'd'}, 'caption': 'wrong'},
            {'voice': {'file_id': 'v', 'file_unique_id': 'v', 'duration': 1}},
            {'sticker': {'file_id': 's', 'file_unique_id': 's', 'type': 'regular', 'width': 1,
                         'height': 1, 'is_animated': False, 'is_video': False}},
        ]:
            self.assertIn('без вложений', self.texts(await self.dispatch(extra=payload))[0])
        for chat_type in ['group', 'supergroup']:
            self.assertIn('личном чате', self.texts(await self.dispatch('wrong', chat_type=chat_type))[0])
            await self.dispatch('/review', chat_type=chat_type)
        self.assertIn('приглашение', self.texts(await self.dispatch('wrong', user_id=43))[0])
        await self.dispatch('/review', user_id=43)
        self.assertEqual(await sync_to_async(current_question)(self.student.pk), first)
        self.assertEqual(await sync_to_async(Attempt.objects.filter(status='open').count)(), 1)

    async def test_all_files_same_names_order_and_long_condition(self):
        text = '<b>not markup</b>\n' + ('😀x ' * 2400)
        await sync_to_async(Task.objects.filter(pk=self.tasks[0].pk).update)(question=text)
        paths = []
        for content in [b'first', b'second', b'third']:
            file = await sync_to_async(TaskAttachment.objects.create)(task=self.tasks[0],
                         file=SimpleUploadedFile('same.txt', content))
            paths.append(Path(file.file.path))
        outgoing = await self.dispatch('/review')
        self.assertEqual(''.join(self.texts(outgoing)[1:]), text)
        documents = [m.document for m in outgoing if getattr(m, 'document', None)]
        self.assertEqual([Path(d.path) for d in documents], paths)
        self.assertEqual([d.filename for d in documents], ['same.txt'] * 3)
        self.assertTrue(all(len(t.encode('utf-16-le')) // 2 <= 4096 for t in self.texts(outgoing)))
        self.assertEqual([m.__api_method__ for m in outgoing][-3:], ['sendDocument'] * 3)

    async def test_missing_file_and_transport_error_preserve_attempt_and_safe_logs(self):
        attachment = await sync_to_async(TaskAttachment.objects.create)(task=self.tasks[0], file='tasks/missing.txt')
        from modules.delivery.models import OutgoingMessage
        await self.dispatch('/review')
        first = await sync_to_async(current_question)(self.student.pk)
        failed = await sync_to_async(OutgoingMessage.objects.get)(state='failed')
        self.assertEqual(failed.error, 'attachment')
        self.assertEqual(first.attachments[0].path, attachment.file.name)
        path = Path(settings.MEDIA_ROOT) / attachment.file.name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'restored')
        await self.dispatch('/review', fail_at=1)
        retry = await sync_to_async(OutgoingMessage.objects.get)(state='retry')
        self.assertEqual(retry.error, 'network')
        # Wait is represented by advancing the saved deadline, not real sleep.
        await sync_to_async(OutgoingMessage.objects.filter(pk=retry.pk).update)(next_attempt_at=NOW)
        await self.dispatch('/review')
        self.assertEqual(await sync_to_async(current_question)(self.student.pk), first)
        self.assertEqual(await sync_to_async(Attempt.objects.count)(), 1)
        self.assertEqual(await sync_to_async(TopicProgress.objects.filter(interval_step=0).count)(), 2)

    async def test_late_cancelled_answer_is_discarded_before_next_question(self):
        await self.dispatch('/review')
        first = await sync_to_async(current_question)(self.student.pk)
        await sync_to_async(assign_topics)(self.student.pk, [self.topics[1].pk], now=NOW)
        outgoing = self.texts(await self.dispatch('secret-reference-1'))
        self.assertIn('отменено', outgoing[0])
        self.assertEqual(outgoing[-1], 'Question 1')
        current = await sync_to_async(current_question)(self.student.pk)
        self.assertNotEqual(current.attempt_id, first.attempt_id)
        self.assertEqual(await sync_to_async(Attempt.objects.filter(status='correct').count)(), 0)
        self.assertEqual(await sync_to_async(TopicProgress.objects.filter(interval_step=0).count)(), 2)
        outgoing = self.texts(await self.dispatch('wrong'))
        self.assertIn('отменено: 1', outgoing[-1])

    async def test_checker_unavailable_and_technical_error_are_not_wrong_answers(self):
        await self.dispatch('/review')
        from modules.study_sessions.services import Review
        with patch('modules.delivery.application.accept_answer', return_value=Review('unavailable')):
            self.assertIn('недоступна', self.texts(await self.dispatch('text'))[0])
        with patch('modules.study_sessions.services.apply_result', side_effect=RuntimeError('private answer')):
            with self.assertRaises(RuntimeError):
                await self.dispatch('text')
        self.assertEqual(await sync_to_async(Attempt.objects.filter(status='open').count)(), 1)
        self.assertEqual(await sync_to_async(TopicProgress.objects.filter(interval_step=0).count)(), 2)

    async def test_empty_queue_and_only_skipped_have_meaningful_summary(self):
        await sync_to_async(assign_topics)(self.student.pk, [], now=NOW)
        text = self.texts(await self.dispatch('/review'))
        self.assertIn('Заданий для ответа сейчас нет', text[-1])
        await sync_to_async(assign_topics)(self.student.pk, [t.pk for t in self.topics], now=NOW)
        await sync_to_async(Task.objects.all().update)(is_active=False)
        text = self.texts(await self.dispatch('/review'))
        self.assertEqual(sum('нет активных заданий' in t for t in text), 2)
        self.assertIn('Пропущено тем: 2', text[-1])
        self.assertEqual(await sync_to_async(Attempt.objects.count)(), 0)
