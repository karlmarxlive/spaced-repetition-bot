"""Persist accepted results; attempts and event deduplication belong to sessions."""
from django.db import transaction
from django.db.models import F

from modules.users.models import StudentTopic
from modules.repetitions.models import TopicProgress
from modules.repetitions.scheduling import calculate_transition, initial_schedule, moscow_date


class InconsistentProgressError(RuntimeError):
    """An assignment was created outside the application API without progress."""


def require_progress(assignments):
    if assignments.filter(progress__isnull=True).exists():
        raise InconsistentProgressError("Assignment has no progress; repair the inconsistent data explicitly")


def _initialize_progress(assignment, *, now, create):
    """Internal: called only by assign_topics inside its write transaction."""
    schedule = initial_schedule(now=now)
    values = vars(schedule)
    if create:
        TopicProgress.objects.create(assignment=assignment, **values)
    elif TopicProgress.objects.filter(assignment=assignment).update(**values) != 1:
        raise InconsistentProgressError("Assignment has no progress")


@transaction.atomic
def apply_result(assignment_id, correct, *, now):
    # Validate before any writes. The no-op UPDATE acquires SQLite's writer lock
    # before reading state; a surrounding transaction can include a future attempt.
    calculate_transition(0, correct, now=now)
    StudentTopic.objects.filter(pk=assignment_id).update(is_active=F("is_active"))
    assignment = StudentTopic.objects.get(pk=assignment_id)
    if not assignment.is_active:
        raise ValueError("Cannot apply a result to an inactive assignment")
    try:
        progress = TopicProgress.objects.get(assignment=assignment)
    except TopicProgress.DoesNotExist as error:
        raise InconsistentProgressError("Assignment has no progress") from error
    schedule = calculate_transition(progress.interval_step, correct, now=now)
    progress.interval_step = schedule.interval_step
    progress.next_review_date = schedule.next_review_date
    progress.save(update_fields=["interval_step", "next_review_date"])
    return progress


def due_topics(student_id, *, now):
    """Materialized, read-only candidates, including topics with no active tasks."""
    today = moscow_date(now)
    assignments = list(StudentTopic.objects.filter(student_id=student_id, is_active=True)
                       .select_related("topic", "progress")
                       .order_by("progress__next_review_date", "topic__order", "topic_id"))
    result = []
    for assignment in assignments:
        try:
            progress = assignment.progress
        except TopicProgress.DoesNotExist as error:
            raise InconsistentProgressError("Assignment has no progress") from error
        if progress.next_review_date <= today:
            result.append(assignment)
    return result
