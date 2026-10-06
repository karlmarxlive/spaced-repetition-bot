"""Atomic incoming update -> lesson transition -> persistent outgoing parts."""
from django.db import transaction

from modules.delivery.models import IncomingEvent, OutgoingMessage, QuestionDelivery
from modules.delivery.outbox import database_retry, enqueue_review, enqueue_text, resume_chat
from modules.delivery.texts import (EVENT_FAILED_TEXT, GROUP_CHAT_TEXT, GROUP_START_TEXT, REPLIES, RESULT_TEXT,
                                   STALE_REPLY_TEXT, START_TEXT, UNDELIVERED_TEXT, UNSUPPORTED_TEXT)
from modules.study_sessions.models import Attempt
from modules.study_sessions.services import (accept_answer, continue_review, form_daily_review,
                                            has_pending_cancellation, start_review, student_for_telegram)
from modules.users.models import Student
from modules.users.services import register_student
from modules.repetitions.scheduling import moscow_date


def process_event(event, *, now):
    return database_retry(_process_event, event, now=now)


def reject_event(*, bot_id, update_id, chat_id, now):
    """Give up on an update that keeps failing: no study changes, one notice to the chat."""
    return database_retry(_reject_event, bot_id=bot_id, update_id=update_id, chat_id=chat_id, now=now)


@transaction.atomic
def _reject_event(*, bot_id, update_id, chat_id, now):
    recorded, created = IncomingEvent.objects.get_or_create(bot_id=bot_id, update_id=update_id,
                                                          defaults={'received_at': now})
    if not created:
        return False
    recorded.state, recorded.processed_at = 'failed', now
    recorded.save()
    if chat_id is not None:
        enqueue_text(text=EVENT_FAILED_TEXT, bot_id=bot_id, chat_id=chat_id,
                     operation=f'update:{bot_id}:{update_id}', now=now)
    return True


@transaction.atomic
def _process_event(event, *, now):
    bot_id, update_id, chat_id = event['bot_id'], event['update_id'], event['chat_id']
    recorded, created = IncomingEvent.objects.get_or_create(bot_id=bot_id, update_id=update_id,
                                                          defaults={'received_at': now})
    if not created and recorded.state in ('processed', 'failed'):
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
        tell(GROUP_START_TEXT if kind == 'start' else GROUP_CHAT_TEXT)
    elif not user or user.get('is_bot'):
        tell(REPLIES['invalid'])
    elif kind == 'start':
        result = register_student(telegram_id=user['id'], first_name=user['first_name'],
            last_name=user.get('last_name'), username=user.get('username'), payload=event.get('payload'))
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
            tell(UNSUPPORTED_TEXT)
        else:
            attempt = Attempt.objects.filter(student_id=student_id, status='open').first()
            reply_id = event.get('reply_id')
            if reply_id is not None:
                linked = OutgoingMessage.objects.filter(bot_id=bot_id, chat_id=chat_id, message_id=reply_id,
                    state='sent', is_question=True, student_id=student_id).first()
                if linked:
                    recorded.attempt_id, recorded.session_id = linked.attempt_id, linked.session_id
                if not linked or not attempt or linked.attempt_id != attempt.pk:
                    tell(STALE_REPLY_TEXT)
                    attempt = None
            elif attempt is None:
                if has_pending_cancellation(student_id):
                    show(continue_review(student_id, now=now))
                else:
                    tell(RESULT_TEXT['no_attempt'])
            if attempt:
                recorded.attempt_id, recorded.session_id = attempt.pk, attempt.session_id
                delivery = QuestionDelivery.objects.filter(attempt=attempt).first()
                # All parts must be confirmed; message IDs reject queued fast texts.
                ready = (delivery is not None and delivery.state == 'delivered' and
                         (reply_id is not None or event['message_id'] > delivery.last_message_id))
                if not ready:
                    tell(UNDELIVERED_TEXT)
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
