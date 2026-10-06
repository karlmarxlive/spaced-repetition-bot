"""Keep aiogram's update generator suspended until application commit succeeds."""
import asyncio
import logging

from aiogram import Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from asgiref.sync import sync_to_async
from config.logging import log_failure
from deploy.health import clear, fail_once

logger = logging.getLogger('bot')
RETRY_SECONDS = 5
# About a minute of retries; then the update is rejected so it cannot block every chat.
MAX_FAILURES = 12


async def _reject(bot, update):
    from django.utils import timezone
    from modules.delivery.application import reject_event
    chat_id = update.message.chat.id if update.message else None
    try:
        await sync_to_async(reject_event, thread_sensitive=True)(
            bot_id=bot.id, update_id=update.update_id, chat_id=chat_id, now=timezone.now())
    except Exception as error:
        log_failure(logger, 'Не удалось отклонить событие', error)
        return False
    return True


class ReliableDispatcher(Dispatcher):
    # Overrides aiogram's private per-update hook; tests pin its signature and caller.
    async def _process_update(self, bot, update, **kwargs):
        name = f'incoming-error-{update.update_id}'
        failures = 0
        while True:
            try:
                await self.feed_update(bot, update, **kwargs)
                clear(name)
                return True
            except TelegramUnauthorizedError:
                raise
            except Exception as error:
                failures += 1
                fail_once(name)
                if failures >= MAX_FAILURES:
                    log_failure(logger, 'Событие не обработано после повторов; отклоняем', error)
                    if await _reject(bot, update):
                        clear(name)
                        return True
                else:
                    log_failure(logger, f'Событие не подтверждено; повтор через {RETRY_SECONDS} секунд', error)
                await asyncio.sleep(RETRY_SECONDS)
