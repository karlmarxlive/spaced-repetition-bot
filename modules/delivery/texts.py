"""Every student-facing reply of the bot, in one place."""

START_TEXT = "Для регистрации нужна ссылка-приглашение учителя. Попросите её у учителя."
REPLIES = {
    "needs_invitation": START_TEXT,
    "registered": "Регистрация выполнена! Пройденные темы назначает учитель. Задания приходят ежедневно; начать вручную можно командой /review.",
    "existing": "Вы уже зарегистрированы. Пройденные темы назначает учитель. Задания приходят ежедневно; начните или продолжите занятие: /review.",
    "unavailable": "Приглашение недоступно. Обратитесь к учителю за новой ссылкой.",
    "invalid": "Не удалось зарегистрироваться. Откройте ссылку учителя из личного аккаунта Telegram.",
}
RESULT_TEXT = {
    "correct": "Верно.",
    "incorrect": "Неверно.",
    "unavailable": "Проверка сейчас недоступна. Ответ не принят; попробуйте позже.",
    "text_required": "Отправьте ответ обычным непустым текстом.",
    "no_attempt": "Нет открытого задания. Начните с /review.",
    "closed": "Этот ответ уже принят. Для продолжения используйте /review.",
    "cancelled": "Задание отменено: тема отключена. Ответ не засчитан.",
}
GROUP_START_TEXT = "Для регистрации откройте ссылку учителя в личном чате с ботом."
GROUP_CHAT_TEXT = "Для занятия откройте бота в личном чате."
UNSUPPORTED_TEXT = "Используйте /review для занятия; ответ отправляйте обычным текстом без вложений."
STALE_REPLY_TEXT = "Ответ на старое или неизвестное задание не принят. Откройте текущий вопрос через /review."
UNDELIVERED_TEXT = "Ответ не принят: дождитесь полной доставки вопроса и ответьте на него через Telegram reply."
EVENT_FAILED_TEXT = "Сообщение не удалось обработать, оно не засчитано. Повторите его позже или отправьте /review."


def summary_text(summary):
    answered = summary.correct + summary.incorrect
    prefix = "Занятие завершено." if answered else "Заданий для ответа сейчас нет."
    text = f"{prefix} Ответов: {answered}; верных: {summary.correct}; неверных: {summary.incorrect}."
    if summary.skipped or summary.cancelled:
        text += f" Пропущено тем: {summary.skipped}; отменено: {summary.cancelled}."
    return text
