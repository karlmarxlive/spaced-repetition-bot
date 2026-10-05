import asyncio
import logging
import os

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from django.core.management.base import BaseCommand, CommandError

from config.logging import log_failure
from modules.delivery.scheduler import run_scheduler

logger = logging.getLogger(__name__)


async def run(token, *, once=False):
    async with AiohttpSession(timeout=15) as session:
        bot = Bot(token=token, session=session)
        await run_scheduler(bot, once=once)


class Command(BaseCommand):
    help = "Ежедневная выдача: отдельный процесс, проверка каждые 30 секунд."
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
        except KeyboardInterrupt:
            logger.info("Остановка планировщика по Ctrl+C")
        except Exception as error:
            log_failure(logger, "Ошибка планировщика", error)
            raise CommandError("Планировщик остановлен из-за ошибки. См. безопасную диагностику выше.") from None
        finally:
            logger.info("Планировщик остановлен; сетевая сессия закрыта")
