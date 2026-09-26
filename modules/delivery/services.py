"""Immediate Telegram delivery, outside database transactions; no background retries."""
from pathlib import Path

from aiogram.types import FSInputFile
from django.conf import settings


def text_parts(text, limit=4096):
    """Preserve every character; count UTF-16 units conservatively for Telegram."""
    part, units = [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            yield "".join(part)
            part, units = [], 0
        part.append(char)
        units += size
    if part:
        yield "".join(part)


async def send_text(message, text):
    for part in text_parts(text):
        await message.answer(part, parse_mode=None)


async def send_review(message, review):
    for notice in review.notices:
        await send_text(message, notice)
    if review.question:
        question = review.question
        await send_text(message, f"Тема: {question.topic}")
        await send_text(message, question.text)
        root = Path(settings.MEDIA_ROOT).resolve()
        for attachment in question.attachments:
            path = (root / attachment.path).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise FileNotFoundError("Snapshot attachment unavailable")
            await message.answer_document(FSInputFile(path, filename=attachment.name), parse_mode=None)
    if review.summary:
        summary = review.summary
        prefix = "Занятие завершено." if summary.correct + summary.incorrect else "Заданий для ответа сейчас нет."
        text = (f"{prefix} Ответов: {summary.correct + summary.incorrect}; "
                f"верных: {summary.correct}; неверных: {summary.incorrect}.")
        if summary.skipped or summary.cancelled:
            text += f" Пропущено тем: {summary.skipped}; отменено: {summary.cancelled}."
        await send_text(message, text)


RESULT_TEXT = {
    "correct": "Верно.",
    "incorrect": "Неверно.",
    "unavailable": "Проверка сейчас недоступна. Ответ не принят; попробуйте позже.",
    "text_required": "Отправьте ответ обычным непустым текстом.",
    "no_attempt": "Нет открытого задания. Начните с /review.",
    "closed": "Этот ответ уже принят. Для продолжения используйте /review.",
    "cancelled": "Задание отменено: тема отключена. Ответ не засчитан.",
}


async def send_result(message, result):
    # Checker feedback is intentionally not transported: it could expose a reference.
    await send_text(message, RESULT_TEXT[result.status])
