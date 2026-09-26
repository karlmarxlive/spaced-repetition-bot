"""Настройки повторений. Все интервалы заданы в календарных днях.

После изменения перезапустите процессы. Изменение числа ступеней требует
makemigrations и migrate; перед сокращением перенесите существующий прогресс
с удаляемых ступеней. Уже сохранённые даты автоматически не пересчитываются.
"""

# Ошибка сбрасывает прогресс на ступень 0.
ERROR_RETRY_DAYS = 1

# Ступени 1, 2, ...: интервалы после последовательных верных ответов.
# На последней ступени повторяется последний интервал.
SUCCESS_INTERVAL_DAYS = (1, 3, 7, 14, 30, 60)


def validate_intervals(error_days, success_days):
    if not isinstance(success_days, (tuple, list)) or not 1 <= len(success_days) <= 10:
        raise ValueError("SUCCESS_INTERVAL_DAYS must contain 1 to 10 intervals")
    if any(type(days) is not int or days <= 0 for days in (error_days, *success_days)):
        raise ValueError("Repetition intervals must be positive integer numbers of days")


validate_intervals(ERROR_RETRY_DAYS, SUCCESS_INTERVAL_DAYS)
MAX_STEP = len(SUCCESS_INTERVAL_DAYS)
