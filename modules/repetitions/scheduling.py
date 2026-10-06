"""Deterministic calendar rules, independent of Django and database state."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config import repetitions as policy

MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class Schedule:
    interval_step: int
    next_review_date: date


def moscow_date(now):
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(MOSCOW).date()


def initial_schedule(*, now):
    return Schedule(0, moscow_date(now))


def calculate_transition(interval_step, correct, *, now):
    today = moscow_date(now)
    step = min(interval_step + 1, policy.MAX_STEP) if correct else 0
    days = policy.SUCCESS_INTERVAL_DAYS[step - 1] if correct else policy.ERROR_RETRY_DAYS
    return Schedule(step, today + timedelta(days=days))
