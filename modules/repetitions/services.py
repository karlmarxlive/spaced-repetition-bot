"""Persist accepted results; attempts and event deduplication belong to sessions."""
from django.db import transaction

from modules.users.models import StudentTopic
from modules.repetitions.models import TopicProgress
from modules.repetitions.scheduling import calculate_transition, initial_schedule, moscow_date


def reset_progress(assignment, *, now):
    """Internal: called only by assign_topics inside its write transaction."""
    TopicProgress.objects.update_or_create(assignment=assignment, defaults=vars(initial_schedule(now=now)))


@transaction.atomic
def apply_result(assignment_id, correct, *, now):
    assignment = StudentTopic.objects.get(pk=assignment_id)
    if not assignment.is_active:
        raise ValueError("Cannot apply a result to an inactive assignment")
    progress = TopicProgress.objects.get(assignment=assignment)
    schedule = calculate_transition(progress.interval_step, correct, now=now)
    progress.interval_step = schedule.interval_step
    progress.next_review_date = schedule.next_review_date
    progress.save(update_fields=["interval_step", "next_review_date"])
    return progress


def due_topics(student_id, *, now):
    """Materialized, read-only candidates, including topics with no active tasks."""
    today = moscow_date(now)
    assignments = (StudentTopic.objects.filter(student_id=student_id, is_active=True)
                   .select_related("topic", "progress")
                   .order_by("progress__next_review_date", "topic__order", "topic_id"))
    return [assignment for assignment in assignments if assignment.progress.next_review_date <= today]
