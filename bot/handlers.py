from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

START_TEXT = "Привет! Я помогу повторять информатику для ЕГЭ. Регистрация и задания появятся на следующих этапах"


async def start(message: Message):
    await message.answer(START_TEXT)


def create_router():
    router = Router(name="start")
    router.message.register(start, CommandStart())
    return router
