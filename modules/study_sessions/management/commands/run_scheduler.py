import asyncio
import logging
import os
import signal
import threading

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from django.core.management.base import BaseCommand, CommandError

from config.logging import log_failure
from modules.delivery.scheduler import run_scheduler

logger = logging.getLogger(__name__)


async def run(token, *, once=False):
    loop = asyncio.get_running_loop()
    handle_signals = os.name == "posix" and threading.current_thread() is threading.main_thread()
    if handle_signals:
        loop.add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    try:
        async with AiohttpSession(timeout=15) as session:
            bot = Bot(token=token, session=session)
            await run_scheduler(bot, once=once)
    finally:
        if handle_signals:
            loop.remove_signal_handler(signal.SIGTERM)


class Command(BaseCommand):
    help = "Ежедневная выдача: отдельный процесс, проверка каждые 2 секунды."
    requires_migrations_checks = True

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Один проход с реальной отправкой, без ожидания.")

    def handle(self, *args, **options):
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise CommandError("Не задан TELEGRAM_BOT_TOKEN. Укажите токен в локальном .env.")
        logger.info("Планировщик запущен; остановка — Ctrl+C")
        try:
            asyncio.run(run(token, once=options["once"]))
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Остановка планировщика по сигналу")
        except Exception as error:
            log_failure(logger, "Ошибка планировщика", error)
            raise CommandError("Планировщик остановлен из-за ошибки. См. безопасную диагностику выше.") from None
        finally:
            logger.info("Планировщик остановлен; сетевая сессия закрыта")
