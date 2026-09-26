"""Persisted manual review. All returned objects are materialized and reference-free."""
from dataclasses import dataclass
from pathlib import PurePosixPath

from django.db import transaction
from django.db.models import F

from modules.checking.services import Checker, check_exact
from modules.materials.models import Task
from modules.repetitions.scheduling import moscow_date
from modules.repetitions.services import apply_result, due_topics
from modules.study_sessions.models import Attempt, StudySession, TaskCursor
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


@transaction.atomic
def start_review(student_id, *, now):
    moscow_date(now)
    _lock_student(student_id)
    session, _ = StudySession.objects.get_or_create(student_id=student_id, state="open",
                                                   defaults={"started_at": now})
    notices = []
    cancelled = session.attempts.filter(status="cancelled", cancellation_seen=False)
    for attempt in cancelled:
        notices.append(f"Тема «{attempt.topic_title}» отключена. Задание отменено без изменения прогресса.")
    cancelled.update(cancellation_seen=True)
    attempt = session.attempts.filter(status="open").first()
    if attempt:
        return Review("question", _question(attempt), notices=tuple(notices))
    processed = set(session.attempts.values_list("assignment_id", flat=True))
    processed.update(item["assignment_id"] for item in session.skipped)
    for assignment in due_topics(student_id, now=now):
        if assignment.pk in processed:
            continue
        task = _choose_task(assignment.pk, assignment.topic_id)
        if task is None:
            session.skipped.append({"assignment_id": assignment.pk, "topic": assignment.topic.title})
            notices.append(f"Тема «{assignment.topic.title}» пропущена: нет активных заданий.")
            continue
        attempt = Attempt.objects.create(
            session=session, student_id=student_id, assignment=assignment, task=task,
            opened_at=now, question=task.question, reference=task.answer,
            topic_title=assignment.topic.title, task_order=task.order,
            attachments=[{"path": item.file.name, "name": PurePosixPath(item.file.name).name}
                         for item in task.attachments.order_by("pk")],
        )
        session.save(update_fields=["skipped"])
        return Review("question", _question(attempt), notices=tuple(notices))
    session.state, session.finished_at = "finished", now
    session.save(update_fields=["state", "finished_at", "skipped"])
    statuses = list(session.attempts.values_list("status", flat=True))
    return Review("finished", summary=Summary(statuses.count("correct"), statuses.count("incorrect"),
                                              len(session.skipped), statuses.count("cancelled")),
                  notices=tuple(notices))


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
        return Review("cancelled")
    result = checker(response, attempt.reference)
    if result.status not in ("correct", "incorrect"):
        return Review("unavailable")
    attempt.response, attempt.status, attempt.finished_at = response, result.status, now
    attempt.save(update_fields=["response", "status", "finished_at"])
    apply_result(attempt.assignment_id, result.status == "correct", now=now)
    TaskCursor.objects.update_or_create(assignment_id=attempt.assignment_id,
        defaults={"last_task_id": attempt.task_id, "last_order": attempt.task_order})
    return Review(result.status)


def has_pending_cancellation(student_id):
    return Attempt.objects.filter(student_id=student_id, session__state="open", status="cancelled",
                                  cancellation_seen=False).exists()
