"""Assignment hooks, called explicitly inside assign_topics' transaction.

This module imports models only, so users and session services do not form a cycle.
"""
from modules.study_sessions.models import Attempt, ReviewQueueItem, TaskCursor


def cancel_assignments(assignment_ids, *, now):
    Attempt.objects.filter(assignment_id__in=assignment_ids, status="open").update(
        status="cancelled", finished_at=now)
    ReviewQueueItem.objects.filter(assignment_id__in=assignment_ids, state__in=["pending", "open"]).update(
        state="cancelled", finished_at=now)


def reset_cursor(assignment_id):
    TaskCursor.objects.filter(assignment_id=assignment_id).delete()
