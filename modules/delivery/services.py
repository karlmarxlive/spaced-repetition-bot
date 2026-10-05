"""Reference-free text formatting shared by transactional delivery."""

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


RESULT_TEXT = {
    "correct": "Верно.",
    "incorrect": "Неверно.",
    "unavailable": "Проверка сейчас недоступна. Ответ не принят; попробуйте позже.",
    "text_required": "Отправьте ответ обычным непустым текстом.",
    "no_attempt": "Нет открытого задания. Начните с /review.",
    "closed": "Этот ответ уже принят. Для продолжения используйте /review.",
    "cancelled": "Задание отменено: тема отключена. Ответ не засчитан.",
}
