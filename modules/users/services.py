"""Synchronous application operations; no Telegram objects or network effects."""
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from modules.materials.models import Topic
from modules.users.models import Invitation, Student, StudentTopic
from modules.repetitions.services import reset_progress
from modules.study_sessions.lifecycle import cancel_assignments, reset_cursor


@dataclass(frozen=True)
class RegistrationResult:
    status: str
    student_id: int | None = None


@transaction.atomic
def register_student(*, telegram_id, first_name, last_name="", username="", payload=None):
    fields = {"first_name": first_name, "last_name": last_name or "", "username": username or ""}
    # A repeated /start refreshes the Telegram names of an existing student.
    if Student.objects.filter(telegram_id=telegram_id).update(**fields):
        return RegistrationResult("existing", Student.objects.only("pk").get(telegram_id=telegram_id).pk)
    if not payload:
        return RegistrationResult("needs_invitation")
    invitation = Invitation.objects.filter(token=payload, is_revoked=False, used_at__isnull=True).first()
    if invitation is None:
        return RegistrationResult("unavailable")
    student = Student.objects.create(telegram_id=telegram_id, course_id=invitation.course_id, **fields)
    Invitation.objects.filter(pk=invitation.pk).update(student=student, used_at=timezone.now())
    return RegistrationResult("registered", student.pk)


def revoke_invitation(invitation_id):
    return bool(Invitation.objects.filter(pk=invitation_id, used_at__isnull=True,
                                         student__isnull=True, is_revoked=False).update(is_revoked=True))


@transaction.atomic
def assign_topics(student_id, topic_ids, *, now):
    """Replace active selections while preserving rows and unchanged timestamps."""
    selected_ids = list(topic_ids)
    if any(type(item) is not int or item <= 0 for item in selected_ids):
        raise ValidationError("ID темы должен быть положительным целым числом.")
    selected = set(selected_ids)
    student = Student.objects.get(pk=student_id)
    allowed = set(Topic.objects.filter(pk__in=selected, course_id=student.course_id).values_list("pk", flat=True))
    if selected != allowed:
        raise ValidationError("Можно назначить только темы курса ученика.")
    assignments = {item.topic_id: item for item in StudentTopic.objects.filter(student=student)}
    for topic_id in selected:
        existing = assignments.get(topic_id)
        if existing is None:
            existing = StudentTopic.objects.create(student=student, topic_id=topic_id, activated_at=now)
            reset_progress(existing, now=now)
        elif not existing.is_active:
            StudentTopic.objects.filter(pk=existing.pk).update(is_active=True, activated_at=now)
            reset_progress(existing, now=now)
            reset_cursor(existing.pk)
    disabled = StudentTopic.objects.filter(student=student, is_active=True).exclude(topic_id__in=selected)
    cancel_assignments(list(disabled.values_list("pk", flat=True)), now=now)
    disabled.update(is_active=False)
