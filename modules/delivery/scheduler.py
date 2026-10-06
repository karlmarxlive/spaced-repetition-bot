"""Daily batch orchestration. No polling, no network inside DB transactions."""
import asyncio
import logging

from aiogram.exceptions import TelegramUnauthorizedError

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from django.utils import timezone

from config.logging import log_failure
from deploy.health import mark, fail_once, clear
from modules.delivery.application import daily_review
from modules.delivery.outbox import drain
from modules.repetitions.scheduling import MOSCOW, moscow_date
from modules.study_sessions.models import DailyReviewRun, DailySchedule
from modules.users.models import Student

logger = logging.getLogger(__name__)


def _candidates(now):
    close_old_connections()
    today = moscow_date(now)
    if now.astimezone(MOSCOW).time() < DailySchedule.objects.get(pk=1).delivery_time:
        return []
    processed = DailyReviewRun.objects.filter(local_date=today).values("student_id")
    return list(Student.objects.exclude(pk__in=processed).order_by("pk").values_list("pk", "telegram_id"))


def _form(student_id, now, bot_id):
    close_old_connections()
    return daily_review(student_id, now=now, bot_id=bot_id)


async def run_tick(bot, *, now, live_delivery=False):
    """Form daily state and independently recover pending delivery on every tick."""
    formed = 0
    failed = False
    for student_id, telegram_id in await sync_to_async(_candidates, thread_sensitive=True)(now):
        try:
            formed += await sync_to_async(_form, thread_sensitive=True)(student_id, now, bot.id)
        except Exception as error:
            failed = True
            log_failure(logger, f"Ошибка ежедневной выдачи ученику {student_id}", error)
    await drain(bot, now=None if live_delivery else now)
    if failed:
        fail_once("scheduler-error")
    else:
        clear("scheduler-error")
    return formed


async def run_scheduler(bot, *, once=False):
    while True:
        try:
            await run_tick(bot, now=timezone.now(), live_delivery=True)
            mark("scheduler")
        except TelegramUnauthorizedError:
            raise
        except Exception as error:
            fail_once("scheduler-error")
            log_failure(logger, "Ошибка прохода планировщика", error)
            if once:
                raise
        if once:
            return
        await asyncio.sleep(2)
