import argparse
import asyncio
import logging
import logging.config
import os

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from bot.handlers import create_router
from config.environment import load_environment
from config.logging import LOGGING

logger = logging.getLogger("bot")


async def run(token, check=False):
    # Own the session even if bot validation, getMe or polling raises.
    async with AiohttpSession(timeout=15) as session:
        bot = Bot(token=token, session=session)
        if check:
            await bot.get_me()
            logger.info("getMe: подключение к Telegram успешно")
            return
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router())
        logger.info("Бот запущен: long polling; остановка — Ctrl+C")
        # asyncio.run handles Ctrl+C on Windows; aiogram handles POSIX signals.
        await dispatcher.start_polling(bot, close_bot_session=False)


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
        asyncio.run(run(token, check=args.check))
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")
    except Exception as error:
        # No external exception text: it may contain a request URL or credentials.
        logger.error("Ошибка запуска/работы бота (%s). Проверьте токен и сеть.", type(error).__name__)
        return 1
    finally:
        logger.info("Бот остановлен; сетевая сессия закрыта")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
