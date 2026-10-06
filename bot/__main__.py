import argparse
import asyncio
import logging
import logging.config
import os

from aiogram import Bot
from bot.polling import ReliableDispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError

from config.environment import load_environment
from config.logging import LOGGING, log_failure

logger = logging.getLogger("bot")


async def run(token, check=False):
    # Own the session even if bot validation, getMe or polling raises.
    async with AiohttpSession(timeout=15) as session:
        bot = Bot(token=token, session=session)
        if check:
            await bot.get_me()
            logger.info("getMe: подключение к Telegram успешно")
            return
        from aiogram.methods import GetUpdates
        from deploy.health import mark

        async def activity(make_request, bot, method):
            result = await make_request(bot, method)
            if isinstance(method, GetUpdates):
                mark("polling")
            return result

        session.middleware(activity)
        dispatcher = ReliableDispatcher()
        from bot.handlers import create_router

        dispatcher.include_router(create_router())
        logger.info("Бот запущен: long polling; остановка — Ctrl+C")
        # asyncio.run handles Ctrl+C on Windows; aiogram handles POSIX signals.
        await dispatcher.start_polling(bot, close_bot_session=False, handle_as_tasks=False)


def main():
    parser = argparse.ArgumentParser(description="Telegram-бот для подготовки к ЕГЭ")
    parser.add_argument("--check", action="store_true", help="Только getMe, без polling")
    args = parser.parse_args()
    load_environment()
    logging.config.dictConfig(LOGGING)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error("Не задан TELEGRAM_BOT_TOKEN. Укажите токен в локальном .env.")
        return 1
    try:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django

        django.setup()
    except Exception as error:
        log_failure(logger, "Ошибка настройки Django. Проверьте конфигурацию проекта", error)
        return 1
    try:
        asyncio.run(run(token, check=args.check))
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")
    except (TokenValidationError, TelegramUnauthorizedError) as error:
        log_failure(logger, "Telegram отклонил токен. Проверьте TELEGRAM_BOT_TOKEN", error)
        return 1
    except TelegramNetworkError as error:
        log_failure(logger, "Ошибка соединения с Telegram. Проверьте сеть", error)
        return 1
    except Exception as error:
        log_failure(logger, "Ошибка работы бота. Проверьте указанное место в коде", error)
        return 1
    finally:
        logger.info("Бот остановлен; сетевая сессия закрыта")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
