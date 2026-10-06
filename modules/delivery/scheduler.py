"""Daily batch orchestration. No polling, no network inside DB transactions."""
import asyncio
from datetime import timedelta
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
STUDENT_RETRY_SECONDS = 60


def _candidates(now):
    close_old_connections()
    today = moscow_date(now)
    if now.astimezone(MOSCOW).time() < DailySchedule.objects.get(pk=1).delivery_time:
        return []
    processed = DailyReviewRun.objects.filter(local_date=today).values("student_id")
    return list(Student.objects.exclude(pk__in=processed).order_by("pk").values_list("pk", flat=True))


def _form(student_id, now, bot_id):
    close_old_connections()
    return daily_review(student_id, now=now, bot_id=bot_id)


async def run_tick(bot, *, now, live_delivery=False, retry_at=None):
    """Form daily state and independently recover pending delivery on every tick.

    retry_at keeps failed students between ticks: they are retried after a pause.
    """
    retry_at = {} if retry_at is None else retry_at
    formed = 0
    candidates = await sync_to_async(_candidates, thread_sensitive=True)(now)
    for student_id in set(retry_at) - set(candidates):
        del retry_at[student_id]
    for student_id in candidates:
        if retry_at.get(student_id, now) > now:
            continue
        try:
            formed += await sync_to_async(_form, thread_sensitive=True)(student_id, now, bot.id)
            retry_at.pop(student_id, None)
        except Exception as error:
            retry_at[student_id] = now + timedelta(seconds=STUDENT_RETRY_SECONDS)
            log_failure(logger, f"Ошибка ежедневной выдачи ученику {student_id}; повтор через "
                                f"{STUDENT_RETRY_SECONDS} секунд", error)
    await drain(bot, now=None if live_delivery else now)
    if retry_at:
        fail_once("scheduler-error")
    else:
        clear("scheduler-error")
    return formed


async def run_scheduler(bot, *, once=False):
    retry_at = {}
    while True:
        try:
            await run_tick(bot, now=timezone.now(), live_delivery=True, retry_at=retry_at)
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
