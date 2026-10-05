import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from asgiref.sync import sync_to_async
from django.utils import timezone

from config.logging import log_failure
from modules.delivery.services import send_review, send_result, send_text
from modules.study_sessions.services import (accept_answer, continue_review, current_question, has_pending_cancellation,
                                             start_review, student_for_telegram)
from modules.users.services import register_student

logger = logging.getLogger(__name__)

START_TEXT = "Для регистрации нужна ссылка-приглашение учителя. Попросите её у учителя."
REPLIES = {
    "needs_invitation": START_TEXT,
    "registered": "Регистрация выполнена! Пройденные темы назначает учитель. Задания приходят ежедневно; начать вручную можно командой /review.",
    "existing": "Вы уже зарегистрированы. Пройденные темы назначает учитель. Задания приходят ежедневно; начните или продолжите занятие: /review.",
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


async def _student(message):
    if message.chat.type != "private":
        await send_text(message, "Для занятия откройте бота в личном чате.")
        return None
    if message.from_user is None or message.from_user.is_bot:
        await send_text(message, REPLIES["invalid"])
        return None
    student_id = await sync_to_async(student_for_telegram, thread_sensitive=True)(message.from_user.id)
    if student_id is None:
        await send_text(message, START_TEXT)
    return student_id


async def _continue(message, student_id, *, manual=False, session_id=None):
    operation = start_review if manual else continue_review
    kwargs = {} if manual else {"session_id": session_id}
    review = await sync_to_async(operation, thread_sensitive=True)(student_id, now=timezone.now(), **kwargs)
    await send_review(message, review)


async def _failure(message, error):
    log_failure(logger, "Ошибка учебного цикла", error)
    try:
        await send_text(message, "Не удалось завершить операцию или отправку. Для продолжения используйте /review.")
    except Exception as delivery_error:
        log_failure(logger, "Не удалось отправить уведомление", delivery_error)


async def review(message: Message):
    try:
        student_id = await _student(message)
        if student_id is not None:
            await _continue(message, student_id, manual=True)
    except Exception as error:
        await _failure(message, error)


async def answer(message: Message):
    try:
        student_id = await _student(message)
        if student_id is None:
            return
        question = await sync_to_async(current_question, thread_sensitive=True)(student_id)
        if question is None:
            if await sync_to_async(has_pending_cancellation, thread_sensitive=True)(student_id):
                # Discard this late answer before opening any next question.
                await _continue(message, student_id)
            else:
                await send_text(message, "Нет открытого задания. Начните с /review.")
            return
        result = await sync_to_async(accept_answer, thread_sensitive=True)(
            student_id, question.attempt_id, message.text, now=timezone.now())
        await send_result(message, result)
        if result.status in ("correct", "incorrect", "cancelled"):
            await _continue(message, student_id, session_id=result.session_id)
    except Exception as error:
        await _failure(message, error)


async def unsupported(message: Message):
    try:
        if await _student(message) is not None:
            await send_text(message, "Используйте /review для занятия; ответ отправляйте обычным текстом без вложений.")
    except Exception as error:
        await _failure(message, error)


def create_router():
    router = Router(name="start")
    router.message.register(start, CommandStart())
    router.message.register(review, Command("review"))
    router.message.register(unsupported, F.text.lstrip().startswith("/"))
    router.message.register(answer, F.text)
    router.message.register(unsupported)
    return router
