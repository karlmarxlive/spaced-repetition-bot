"""Test-only read of the persisted current question."""
from modules.study_sessions.models import Attempt
from modules.study_sessions.services import _question


def current_question(student_id):
    attempt = Attempt.objects.filter(student_id=student_id, status="open").first()
    return _question(attempt) if attempt else None
