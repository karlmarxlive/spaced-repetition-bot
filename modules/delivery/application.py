"""Atomic incoming update -> lesson transition -> persistent outgoing parts."""
from django.db import OperationalError, transaction
from django.db.models import F

from bot.texts import REPLIES, START_TEXT
from modules.delivery.models import IncomingEvent, OutgoingMessage, QuestionDelivery
from modules.delivery.outbox import database_retry, enqueue_review, enqueue_text, resume_chat
from modules.delivery.services import RESULT_TEXT
from modules.study_sessions.models import Attempt
from modules.study_sessions.services import (accept_answer, continue_review, form_daily_review,
                                            has_pending_cancellation, start_review, student_for_telegram)
from modules.users.models import Student
from modules.users.services import register_student
from modules.repetitions.scheduling import moscow_date


def process_event(event, *, now):
    return database_retry(_process_event, event, now=now)


@transaction.atomic
def _process_event(event, *, now):
    bot_id, update_id, chat_id = event['bot_id'], event['update_id'], event['chat_id']
    # First statement takes SQLite's writer lock even when the update is absent.
    IncomingEvent.objects.filter(bot_id=bot_id, update_id=update_id).update(bot_id=F('bot_id'))
    recorded, created = IncomingEvent.objects.get_or_create(bot_id=bot_id, update_id=update_id,
                                                          defaults={'received_at': now})
    if not created and recorded.state == 'processed':
        return False
    operation = f'update:{bot_id}:{update_id}'
    common = dict(bot_id=bot_id, chat_id=chat_id, operation=operation, now=now)
    def tell(text):
        enqueue_text(text=text, attempt_id=recorded.attempt_id, session_id=recorded.session_id, **common)
    def show(review, manual=False):
        if review.session_id:
            recorded.session_id = review.session_id
        if review.question:
            if recorded.attempt_id is None:
                recorded.attempt_id = review.question.attempt_id
            recorded.session_id = Attempt.objects.get(pk=review.question.attempt_id).session_id
        enqueue_review(review, manual=manual, **common)
    user = event.get('user')
    kind = event['kind']
    if event['chat_type'] != 'private':
        tell('Для регистрации откройте ссылку учителя в личном чате с ботом.' if kind == 'start'
             else 'Для занятия откройте бота в личном чате.')
    elif not user or user.get('is_bot'):
        tell(REPLIES['invalid'])
    elif kind == 'start':
        result = register_student(telegram_id=user['id'], first_name=user['first_name'],
            last_name=user.get('last_name'), username=user.get('username'), payload=event.get('payload'))
        if result.status == 'busy':
            raise OperationalError('database locked')
        if result.status != 'invalid':
            resume_chat(bot_id, chat_id, now=now)
        recorded.student_id = result.student_id
        common['student_id'] = result.student_id
        tell(REPLIES[result.status])
    else:
        student_id = student_for_telegram(user['id'])
        common['student_id'] = recorded.student_id = student_id
        if student_id is None:
            tell(START_TEXT)
        elif kind == 'review':
            resume_chat(bot_id, chat_id, now=now)
            show(start_review(student_id, now=now), manual=True)
        elif kind == 'unsupported':
            tell('Используйте /review для занятия; ответ отправляйте обычным текстом без вложений.')
        else:
            attempt = Attempt.objects.filter(student_id=student_id, status='open').first()
            reply_id = event.get('reply_id')
            if reply_id is not None:
                linked = OutgoingMessage.objects.filter(bot_id=bot_id, chat_id=chat_id, message_id=reply_id,
                    state='sent', is_question=True, student_id=student_id).first()
                if linked:
                    recorded.attempt_id, recorded.session_id = linked.attempt_id, linked.session_id
                if not linked or not attempt or linked.attempt_id != attempt.pk:
                    tell('Ответ на старое или неизвестное задание не принят. Откройте текущий вопрос через /review.')
                    attempt = None
            elif attempt is None:
                if has_pending_cancellation(student_id):
                    show(continue_review(student_id, now=now))
                else:
                    tell(RESULT_TEXT['no_attempt'])
            if attempt:
                recorded.attempt_id, recorded.session_id = attempt.pk, attempt.session_id
                delivery = QuestionDelivery.objects.filter(attempt=attempt).first()
                # Legacy unknown deliveries accept ordinary answers. New deliveries
                # require all parts confirmed; message IDs reject queued fast texts.
                ready = (delivery is None or (delivery.state == 'delivered' and
                         (reply_id is not None or event['message_id'] > delivery.last_message_id)))
                if not ready:
                    tell('Ответ не принят: дождитесь полной доставки вопроса и ответьте на него через Telegram reply.')
                else:
                    result = accept_answer(student_id, attempt.pk, event.get('text'), now=now)
                    tell(RESULT_TEXT[result.status])
                    if result.status in ('correct', 'incorrect', 'cancelled'):
                        show(continue_review(student_id, now=now, session_id=result.session_id))
    recorded.state, recorded.processed_at = 'processed', now
    recorded.save()
    return True


def daily_review(student_id, *, bot_id, now):
    return database_retry(_daily_review, student_id, bot_id=bot_id, now=now)


@transaction.atomic
def _daily_review(student_id, *, bot_id, now):
    review = form_daily_review(student_id, now=now)
    if review.question:
        enqueue_review(review, bot_id=bot_id, chat_id=Student.objects.get(pk=student_id).telegram_id,
                       student_id=student_id, operation=f'daily:{bot_id}:{student_id}:{moscow_date(now)}:{review.question.attempt_id}', now=now)
    return bool(review.question)
