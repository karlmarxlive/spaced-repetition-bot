"""Synchronous application operations; no Telegram objects or network effects."""
from dataclasses import dataclass
import re
import time

from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F
from django.utils import timezone

from modules.materials.models import Topic
from modules.users.models import Invitation, Student, StudentTopic


@dataclass(frozen=True)
class RegistrationResult:
    status: str
    student_id: int | None = None


class InvitationUnavailable(Exception):
    pass


def register_student(*, telegram_id, first_name, last_name="", username="", payload=None):
    if not isinstance(telegram_id, int) or isinstance(telegram_id, bool) or not 0 < telegram_id <= 2**63 - 1:
        return RegistrationResult("invalid")
    fields = {"first_name": first_name, "last_name": last_name or "", "username": username or ""}
    if (not isinstance(first_name, str) or not first_name or len(first_name) > 64 or
            any(not isinstance(fields[key], str) or len(fields[key]) > limit
                for key, limit in (("last_name", 64), ("username", 32)))):
        return RegistrationResult("invalid")
    for attempt in range(4):
        try:
            return _register(telegram_id, fields, payload)
        except InvitationUnavailable:
            return RegistrationResult("unavailable")
        except IntegrityError:
            # The entire attempt rolled back. Re-read on retry, including identity conflicts.
            pass
        except OperationalError as error:
            if "locked" not in str(error).lower() and "busy" not in str(error).lower():
                raise
        if attempt < 3:
            time.sleep(0.03 * (attempt + 1))
    return RegistrationResult("busy")


@transaction.atomic
def _register(telegram_id, fields, payload):
    # First statement is a write, even for an absent ID. SQLite takes the writer
    # lock before any snapshot reads, avoiding deferred read-to-write upgrades.
    if Student.objects.filter(telegram_id=telegram_id).update(**fields):
        return RegistrationResult("existing", Student.objects.only("pk").get(telegram_id=telegram_id).pk)
    if not payload:
        return RegistrationResult("needs_invitation")
    if not isinstance(payload, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload):
        return RegistrationResult("unavailable")
    invitation = Invitation.objects.filter(token=payload, is_revoked=False, used_at__isnull=True).first()
    if invitation is None:
        return RegistrationResult("unavailable")
    student = Student.objects.create(telegram_id=telegram_id, course_id=invitation.course_id, **fields)
    changed = Invitation.objects.filter(pk=invitation.pk, is_revoked=False, used_at__isnull=True,
                                        student__isnull=True).update(student=student, used_at=timezone.now())
    if changed != 1:
        raise InvitationUnavailable
    return RegistrationResult("registered", student.pk)


def revoke_invitation(invitation_id):
    return bool(Invitation.objects.filter(pk=invitation_id, used_at__isnull=True,
                                         student__isnull=True, is_revoked=False).update(is_revoked=True))


@transaction.atomic
def assign_topics(student_id, topic_ids):
    """Replace active selections while preserving rows and unchanged timestamps."""
    selected = set(topic_ids)
    # Serialize assignments for this student; also acquire SQLite's writer lock.
    Student.objects.filter(pk=student_id).update(display_name=F("display_name"))
    student = Student.objects.get(pk=student_id)
    allowed = set(Topic.objects.filter(pk__in=selected, course_id=student.course_id).values_list("pk", flat=True))
    if selected != allowed:
        raise ValidationError("Можно назначить только темы курса ученика.")
    assignments = {item.topic_id: item for item in StudentTopic.objects.filter(student=student)}
    now = timezone.now()
    for topic_id in selected:
        existing = assignments.get(topic_id)
        if existing is None:
            StudentTopic.objects.create(student=student, topic_id=topic_id, activated_at=now)
        elif not existing.is_active:
            StudentTopic.objects.filter(pk=existing.pk).update(is_active=True, activated_at=now)
    StudentTopic.objects.filter(student=student, is_active=True).exclude(topic_id__in=selected).update(is_active=False)
