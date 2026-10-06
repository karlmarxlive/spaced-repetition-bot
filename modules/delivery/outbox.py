"""Transactional enqueue and leased per-chat delivery. Network is outside atomic."""
import asyncio
import logging
from datetime import timedelta
from pathlib import Path
import time
from uuid import uuid4

from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
                                TelegramRetryAfter, TelegramServerError, TelegramUnauthorizedError)
from aiogram.types import FSInputFile
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from modules.delivery.models import OutgoingMessage, QuestionDelivery
from modules.delivery.services import text_parts
from modules.delivery.texts import summary_text
from modules.study_sessions.models import Attempt

LEASE_SECONDS = 60
NETWORK_SECONDS = 20
MAX_ATTEMPTS = 10
logger = logging.getLogger(__name__)


def database_retry(operation, *args, **kwargs):
    for index in range(4):
        try:
            return operation(*args, **kwargs)
        except OperationalError as error:
            if not any(word in str(error).lower() for word in ('locked', 'busy')) or index == 3:
                raise
            time.sleep(0.05 * (index + 1))


def enqueue_text(*, text, **kwargs):
    for part in text_parts(text):
        enqueue_part(text=part, **kwargs)


def enqueue_part(*, bot_id, chat_id, operation, now, **kwargs):
    # Called under the application's SQLite writer transaction.
    part = OutgoingMessage.objects.filter(bot_id=bot_id, operation=operation).count()
    return OutgoingMessage.objects.create(bot_id=bot_id, chat_id=chat_id, operation=operation,
                                          part=part, next_attempt_at=now, **kwargs)


def enqueue_review(review, *, bot_id, chat_id, student_id, operation, now, manual=False):
    common = dict(bot_id=bot_id, chat_id=chat_id, student_id=student_id,
                  session_id=review.session_id, operation=operation, now=now)
    for notice in review.notices:
        enqueue_text(text=notice, **common)
    if review.question:
        question = review.question
        attempt = Attempt.objects.get(pk=question.attempt_id)
        delivery = QuestionDelivery.objects.filter(attempt=attempt).first()
        if delivery and delivery.state != 'delivered':
            # An explicit review resumes failed parts, never creates parallel sets.
            if manual:
                resume_chat(bot_id, chat_id, now=now)
            return
        if delivery and not manual:
            return
        QuestionDelivery.objects.update_or_create(attempt=attempt,
            defaults={'state': 'pending', 'operation': operation, 'delivered_at': None, 'last_message_id': None})
        common.update(attempt_id=attempt.pk, session_id=attempt.session_id, is_question=True)
        enqueue_text(text=f'Тема: {question.topic}', **common)
        enqueue_text(text=question.text, **common)
        for attachment in question.attachments:
            enqueue_part(kind='document', path=attachment.path, filename=attachment.name, **common)
    if review.summary:
        enqueue_text(text=summary_text(review.summary), **common)


def cancel_stale(bot_id, chat_id=None):
    rows = OutgoingMessage.objects.filter(bot_id=bot_id, is_question=True).exclude(state__in=['sent', 'cancelled'])
    if chat_id is not None:
        rows = rows.filter(chat_id=chat_id)
    rows.exclude(attempt__status='open').update(state='cancelled', lease_token=None, lease_until=None)


def resume_chat(bot_id, chat_id, *, now):
    cancel_stale(bot_id, chat_id)
    QuestionDelivery.objects.filter(state='failed', attempt__status='open',
        attempt__student__telegram_id=chat_id).update(state='pending')
    OutgoingMessage.objects.filter(bot_id=bot_id, chat_id=chat_id, state='failed').update(
        state='pending', attempts=0, next_attempt_at=now, error='')


@transaction.atomic
def claim(*, bot_id, now, chat_id=None):
    # Write before reading: real SQLite writer serialization, also across processes.
    OutgoingMessage.objects.filter(bot_id=bot_id, state='sending', lease_until__lte=now).update(
        state='retry', lease_token=None, lease_until=None, next_attempt_at=now)
    cancel_stale(bot_id, chat_id)
    earlier = OutgoingMessage.objects.filter(bot_id=bot_id, chat_id=OuterRef('chat_id'), pk__lt=OuterRef('pk')).exclude(
        state__in=['sent', 'cancelled'])
    rows = OutgoingMessage.objects.filter(bot_id=bot_id, state__in=['pending', 'retry'], next_attempt_at__lte=now)
    if chat_id is not None:
        rows = rows.filter(chat_id=chat_id)
    row = rows.annotate(blocked=Exists(earlier)).filter(blocked=False).first()
    if row is None:
        return None
    token = uuid4()
    changed = OutgoingMessage.objects.filter(pk=row.pk, state=row.state).update(
        state='sending', attempts=F('attempts') + 1, lease_token=token, lease_until=now + timedelta(seconds=LEASE_SECONDS))
    if not changed:
        return None
    row.lease_token, row.attempts = token, row.attempts + 1
    return row


@transaction.atomic
def finish(row, *, now, message_id=None, error='', retry_seconds=None):
    # No stale owner may acknowledge a new owner's send, even after lease expiry.
    rows = OutgoingMessage.objects.filter(pk=row.pk, state='sending', lease_token=row.lease_token, lease_until__gt=now)
    state = 'sent' if not error else ('retry' if retry_seconds is not None and row.attempts < MAX_ATTEMPTS else 'failed')
    if not rows.update(state=state, message_id=message_id, sent_at=now if not error else None,
                       error=error, next_attempt_at=now + timedelta(seconds=retry_seconds or 0),
                       lease_token=None, lease_until=None):
        return False
    if row.is_question and state == 'failed':
        QuestionDelivery.objects.filter(attempt_id=row.attempt_id, operation=row.operation).update(state='failed')
    if row.is_question and state == 'sent':
        delivery = QuestionDelivery.objects.filter(attempt_id=row.attempt_id, operation=row.operation)
        if (Attempt.objects.filter(pk=row.attempt_id, status='open').exists() and
                not OutgoingMessage.objects.filter(bot_id=row.bot_id, operation=row.operation, is_question=True).exclude(state='sent').exists()):
            delivery.update(state='delivered', delivered_at=now, last_message_id=message_id)
    return True


def classify(error, attempts):
    if isinstance(error, TelegramUnauthorizedError):
        return 'configuration', None
    if isinstance(error, TelegramRetryAfter):
        return 'rate_limit', max(1, error.retry_after)
    if isinstance(error, TelegramForbiddenError):
        return 'chat_unavailable', None
    if isinstance(error, FileNotFoundError):
        return 'attachment', None
    if isinstance(error, TelegramBadRequest):
        return 'telegram_request', None
    if isinstance(error, (TelegramNetworkError, TelegramServerError, OSError, TimeoutError)):
        return 'network', min(5 * 2 ** min(attempts - 1, 10), 900)
    return 'transport', None


async def drain(bot, *, now=None, chat_id=None, limit=100):
    """One bounded pass. Failed heads block only their chat, never the whole queue."""
    clock = (lambda: now) if now is not None else timezone.now
    sent = 0
    for _ in range(limit):
        row = await sync_to_async(database_retry, thread_sensitive=True)(claim, bot_id=bot.id, now=clock(), chat_id=chat_id)
        if row is None:
            break
        try:
            if row.kind == 'document':
                root = Path(settings.MEDIA_ROOT).resolve()
                path = (root / row.path).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise FileNotFoundError('Snapshot unavailable')
                request = bot.send_document(row.chat_id, FSInputFile(path, filename=row.filename), parse_mode=None)
            else:
                request = bot.send_message(row.chat_id, row.text, parse_mode=None)
            message = await asyncio.wait_for(request, timeout=NETWORK_SECONDS)
        except Exception as error:
            category, delay = classify(error, row.attempts)
            logger.error('Отправка %s: %s', row.pk, category)
            await sync_to_async(database_retry, thread_sensitive=True)(finish, row, now=clock(), error=category, retry_seconds=delay)
            if category == 'configuration':
                raise
        else:
            await sync_to_async(database_retry, thread_sensitive=True)(finish, row, now=clock(), message_id=message.message_id)
            sent += 1
    return sent
