from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message
from asgiref.sync import sync_to_async

from modules.users.services import register_student

START_TEXT = "Для регистрации нужна ссылка-приглашение учителя. Попросите её у учителя."
REPLIES = {
    "needs_invitation": START_TEXT,
    "registered": "Регистрация выполнена! Пройденные темы назначает учитель. Выдача заданий появится позже.",
    "existing": "Вы уже зарегистрированы. Пройденные темы назначает учитель. Выдача заданий появится позже.",
    "unavailable": "Приглашение недоступно. Обратитесь к учителю за новой ссылкой.",
    "invalid": "Не удалось зарегистрироваться. Откройте ссылку учителя из личного аккаунта Telegram.",
    "busy": "База временно занята. Повторите /start по ссылке учителя через несколько секунд.",
}


async def start(message: Message, command: CommandObject):
    if message.chat.type != "private":
        await message.answer("Для регистрации откройте ссылку учителя в личном чате с ботом.")
        return
    user = message.from_user
    if user is None or user.is_bot:
        await message.answer(REPLIES["invalid"])
        return
    result = await sync_to_async(register_student, thread_sensitive=True)(
        telegram_id=user.id, first_name=user.first_name, last_name=user.last_name,
        username=user.username, payload=command.args,
    )
    await message.answer(REPLIES[result.status])


def create_router():
    router = Router(name="start")
    router.message.register(start, CommandStart())
    return router
