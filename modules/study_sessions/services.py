"""Persisted review queue. All returned objects are materialized and reference-free."""
from dataclasses import dataclass
from pathlib import PurePosixPath

from django.db import transaction
from django.db.models import F

from modules.checking.services import Checker, check_exact
from modules.materials.models import Task
from modules.repetitions.scheduling import MOSCOW, moscow_date
from modules.repetitions.services import apply_result, due_topics
from modules.study_sessions.models import (Attempt, DailyReviewRun, DailySchedule,
                                          ReviewQueueItem, StudySession, TaskCursor)
from modules.users.models import Student


@dataclass(frozen=True)
class Attachment:
    path: str
    name: str


@dataclass(frozen=True)
class Question:
    attempt_id: int
    topic: str
    text: str
    attachments: tuple[Attachment, ...]


@dataclass(frozen=True)
class Summary:
    correct: int
    incorrect: int
    skipped: int
    cancelled: int


@dataclass(frozen=True)
class Review:
    status: str
    question: Question | None = None
    summary: Summary | None = None
    notices: tuple[str, ...] = ()


def _question(attempt):
    return Question(attempt.pk, attempt.topic_title, attempt.question,
                    tuple(Attachment(**item) for item in attempt.attachments))


def student_for_telegram(telegram_id):
    return Student.objects.filter(telegram_id=telegram_id).values_list("pk", flat=True).first()


def current_question(student_id):
    attempt = Attempt.objects.filter(student_id=student_id, status="open").first()
    return _question(attempt) if attempt else None


def _lock_student(student_id):
    # Acquire SQLite writer lock before reading a snapshot. No network inside atomic.
    if not Student.objects.filter(pk=student_id).update(display_name=F("display_name")):
        raise Student.DoesNotExist


def _choose_task(assignment_id, topic_id):
    tasks = [task for task in Task.objects.filter(topic_id=topic_id, is_active=True).order_by("order", "pk")
             if task.question.strip() and task.answer.strip()]
    if not tasks:
        return None
    cursor = TaskCursor.objects.filter(assignment_id=assignment_id).first()
    if cursor:
        for index, task in enumerate(tasks):
            if task.pk == cursor.last_task_id:
                return tasks[(index + 1) % len(tasks)]
        # Moved/disabled last task: continue after the saved opening sort key.
        for task in tasks:
            if (task.order, task.pk) > (cursor.last_order, cursor.last_task_id):
                return task
    return tasks[0]


def _session(student_id, now):
    session, _ = StudySession.objects.get_or_create(student_id=student_id, state="open",
                                                   defaults={"started_at": now})
    return session


def _enqueue(session, *, now, daily=False):
    excluded = set(ReviewQueueItem.objects.filter(
        assignment__student_id=session.student_id, state__in=["pending", "open"]
    ).values_list("assignment_id", flat=True))
    if not daily:
        # A manual traversal does not revisit skipped/cancelled topics until a new
        # session. A daily batch can retry them, even in a multi-day session.
        excluded.update(item["assignment_id"] for item in session.skipped)
        excluded.update(session.attempts.filter(status="cancelled").values_list("assignment_id", flat=True))
    for assignment in due_topics(session.student_id, now=now):
        if assignment.pk not in excluded:
            ReviewQueueItem.objects.create(session=session, assignment=assignment, enqueued_at=now)


def _advance(session, *, now):
    notices = []
    cancelled = session.attempts.filter(status="cancelled", cancellation_seen=False)
    for attempt in cancelled:
        notices.append(f"Тема «{attempt.topic_title}» отключена. Задание отменено без изменения прогресса.")
    cancelled.update(cancellation_seen=True)
    attempt = session.attempts.filter(status="open").first()
    if attempt:
        return Review("question", _question(attempt), notices=tuple(notices))
    for item in session.queue.filter(state="pending").select_related("assignment__topic"):
        assignment = item.assignment
        if not assignment.is_active:
            item.state, item.finished_at = "cancelled", now
            item.save(update_fields=["state", "finished_at"])
            continue
        task = _choose_task(assignment.pk, assignment.topic_id)
        if task is None:
            item.state, item.finished_at = "skipped", now
            item.save(update_fields=["state", "finished_at"])
            session.skipped.append({"assignment_id": assignment.pk, "topic": assignment.topic.title})
            notices.append(f"Тема «{assignment.topic.title}» пропущена: нет активных заданий.")
            continue
        attempt = Attempt.objects.create(
            session=session, student_id=session.student_id, assignment=assignment, task=task,
            opened_at=now, question=task.question, reference=task.answer,
            topic_title=assignment.topic.title, task_order=task.order,
            attachments=[{"path": item.file.name, "name": PurePosixPath(item.file.name).name}
                         for item in task.attachments.order_by("pk")],
        )
        item.state, item.attempt = "open", attempt
        item.save(update_fields=["state", "attempt"])
        session.save(update_fields=["skipped"])
        return Review("question", _question(attempt), notices=tuple(notices))
    session.state, session.finished_at = "finished", now
    session.save(update_fields=["state", "finished_at", "skipped"])
    statuses = list(session.attempts.values_list("status", flat=True))
    return Review("finished", summary=Summary(statuses.count("correct"), statuses.count("incorrect"),
                                              len(session.skipped), statuses.count("cancelled")),
                  notices=tuple(notices))


@transaction.atomic
def start_review(student_id, *, now):
    """Explicit /review: append currently due topics and show the current question."""
    moscow_date(now)
    _lock_student(student_id)
    session = _session(student_id, now)
    _enqueue(session, now=now)
    return _advance(session, now=now)


@transaction.atomic
def continue_review(student_id, *, now):
    """After an answer, consume the persisted queue without forming a new batch."""
    moscow_date(now)
    _lock_student(student_id)
    return _advance(_session(student_id, now), now=now)


@transaction.atomic
def form_daily_review(student_id, *, now):
    """At most one batch per Moscow day. Existing questions are never resent."""
    today = moscow_date(now)
    _lock_student(student_id)
    schedule = DailySchedule.objects.get(pk=1)
    if now.astimezone(MOSCOW).time() < schedule.delivery_time:
        return Review("idle")
    _, created = DailyReviewRun.objects.get_or_create(
        student_id=student_id, local_date=today, defaults={"formed_at": now})
    if not created:
        return Review("idle")
    session = _session(student_id, now)
    _enqueue(session, now=now, daily=True)
    if session.attempts.filter(status="open").exists():
        return Review("idle")
    return _advance(session, now=now)


@transaction.atomic
def accept_answer(student_id, attempt_id, response, *, now, checker: Checker = check_exact):
    """Accept one specifically identified attempt; repeated completion is a no-op."""
    moscow_date(now)
    _lock_student(student_id)
    attempt = Attempt.objects.filter(pk=attempt_id, student_id=student_id).select_related("assignment").first()
    if attempt is None:
        return Review("no_attempt")
    if attempt.status != "open":
        return Review("cancelled" if attempt.status == "cancelled" else "closed")
    if not isinstance(response, str) or not response.strip() or response.lstrip().startswith("/"):
        return Review("text_required")
    if not attempt.assignment.is_active:
        attempt.status, attempt.finished_at = "cancelled", now
        attempt.save(update_fields=["status", "finished_at"])
        ReviewQueueItem.objects.filter(attempt=attempt).update(state="cancelled", finished_at=now)
        return Review("cancelled")
    result = checker(response, attempt.reference)
    if result.status not in ("correct", "incorrect"):
        return Review("unavailable")
    attempt.response, attempt.status, attempt.finished_at = response, result.status, now
    attempt.save(update_fields=["response", "status", "finished_at"])
    ReviewQueueItem.objects.filter(attempt=attempt).update(state="done", finished_at=now)
    apply_result(attempt.assignment_id, result.status == "correct", now=now)
    TaskCursor.objects.update_or_create(assignment_id=attempt.assignment_id,
        defaults={"last_task_id": attempt.task_id, "last_order": attempt.task_order})
    return Review(result.status)


def has_pending_cancellation(student_id):
    return Attempt.objects.filter(student_id=student_id, session__state="open", status="cancelled",
                                  cancellation_seen=False).exists()
