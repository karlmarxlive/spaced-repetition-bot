"""Daily batch orchestration. No polling, no network inside DB transactions."""
import asyncio
import logging

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from django.utils import timezone

from config.logging import log_failure
from modules.delivery.services import send_review
from modules.repetitions.scheduling import MOSCOW, moscow_date
from modules.study_sessions.models import DailyReviewRun, DailySchedule
from modules.study_sessions.services import form_daily_review
from modules.users.models import Student

logger = logging.getLogger(__name__)


class ChatRecipient:
    """The same transport interface as Message, for unsolicited private delivery."""
    def __init__(self, bot, chat_id):
        self.bot, self.chat_id = bot, chat_id

    async def answer(self, text, **kwargs):
        return await self.bot.send_message(self.chat_id, text, **kwargs)

    async def answer_document(self, document, **kwargs):
        return await self.bot.send_document(self.chat_id, document, **kwargs)


def _candidates(now):
    close_old_connections()
    today = moscow_date(now)
    if now.astimezone(MOSCOW).time() < DailySchedule.objects.get(pk=1).delivery_time:
        return []
    processed = DailyReviewRun.objects.filter(local_date=today).values("student_id")
    return list(Student.objects.exclude(pk__in=processed).order_by("pk").values_list("pk", "telegram_id"))


def _form(student_id, now):
    close_old_connections()
    return form_daily_review(student_id, now=now)


async def run_tick(bot, *, now):
    """One deterministic pass; failed formation can be retried on the next tick.

    A transport failure preserves the opened attempt. Stage 7 will add an outbox;
    until then /review is the explicit recovery path for uncertain delivery.
    """
    sent = 0
    for student_id, telegram_id in await sync_to_async(_candidates, thread_sensitive=True)(now):
        try:
            review = await sync_to_async(_form, thread_sensitive=True)(student_id, now)
            if review.question:
                await send_review(ChatRecipient(bot, telegram_id), review)
                sent += 1
        except Exception as error:
            log_failure(logger, f"Ошибка ежедневной выдачи ученику {student_id}", error)
    return sent


async def run_scheduler(bot, *, once=False):
    while True:
        try:
            await run_tick(bot, now=timezone.now())
        except Exception as error:
            log_failure(logger, "Ошибка прохода планировщика", error)
            if once:
                raise
        if once:
            return
        await asyncio.sleep(30)
