"""Keep aiogram's update generator suspended until application commit succeeds."""
import asyncio
import logging

from aiogram import Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from config.logging import log_failure
from deploy.health import directory, mark
import os

logger = logging.getLogger('bot')


class ReliableDispatcher(Dispatcher):
    async def _process_update(self, bot, update, **kwargs):
        while True:
            try:
                await self.feed_update(bot, update, **kwargs)
                if os.environ.get("APP_MANAGED") == "1":
                    (directory() / f"incoming-error-{update.update_id}").unlink(missing_ok=True)
                return True
            except TelegramUnauthorizedError:
                raise
            except Exception as error:
                name = f'incoming-error-{update.update_id}'
                if not (directory() / name).exists():
                    mark(name)
                log_failure(logger, 'Событие не подтверждено; повтор через 5 секунд', error)
                await asyncio.sleep(5)
