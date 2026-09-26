"""Exact short-answer checker. No persistence or transport dependencies."""
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class CheckResult:
    status: Literal["correct", "incorrect", "unavailable"]
    feedback: str | None = None


class Checker(Protocol):
    def __call__(self, response: str, reference: str) -> CheckResult: ...


def check_exact(response: str, reference: str) -> CheckResult:
    if not reference.strip():
        return CheckResult("unavailable")
    return CheckResult("correct" if response.strip() == reference.strip() else "incorrect")
