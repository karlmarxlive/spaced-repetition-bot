from datetime import time

from django.db import models


class StudySession(models.Model):
    student = models.ForeignKey("users.Student", on_delete=models.PROTECT)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    state = models.CharField(max_length=10, default="open")
    # Historical and current skip notices, retained for the session summary.
    skipped = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["student"], condition=models.Q(state="open"),
                                    name="one_open_session_per_student"),
            models.CheckConstraint(condition=(models.Q(state="open", finished_at__isnull=True) |
                                              models.Q(state="finished", finished_at__isnull=False)),
                                   name="session_consistent_state"),
        ]


class Attempt(models.Model):
    student = models.ForeignKey("users.Student", on_delete=models.PROTECT)
    session = models.ForeignKey(StudySession, on_delete=models.PROTECT, related_name="attempts")
    assignment = models.ForeignKey("users.StudentTopic", on_delete=models.PROTECT)
    task = models.ForeignKey("materials.Task", on_delete=models.PROTECT)
    opened_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=10, default="open")
    response = models.TextField(null=True)
    question = models.TextField()
    reference = models.TextField()
    topic_title = models.CharField(max_length=200)
    attachments = models.JSONField(default=list)
    task_order = models.PositiveIntegerField()
    cancellation_seen = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["student"], condition=models.Q(status="open"),
                                    name="one_open_attempt_per_student"),
            models.CheckConstraint(condition=(
                models.Q(status="open", finished_at__isnull=True, response__isnull=True) |
                models.Q(status="cancelled", finished_at__isnull=False, response__isnull=True) |
                models.Q(status__in=["correct", "incorrect"], finished_at__isnull=False, response__isnull=False)
            ), name="attempt_consistent_state"),
        ]


class TaskCursor(models.Model):
    assignment = models.OneToOneField("users.StudentTopic", on_delete=models.PROTECT)
    last_task = models.ForeignKey("materials.Task", on_delete=models.PROTECT)
    last_order = models.PositiveIntegerField()


class ReviewQueueItem(models.Model):
    session = models.ForeignKey(StudySession, on_delete=models.PROTECT, related_name="queue")
    assignment = models.ForeignKey("users.StudentTopic", on_delete=models.PROTECT)
    attempt = models.OneToOneField(Attempt, on_delete=models.PROTECT, null=True, related_name="queue_item")
    enqueued_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    state = models.CharField(max_length=10, default="pending", choices=[
        ("pending", "В очереди"), ("open", "Открыто"), ("done", "Завершено"),
        ("skipped", "Пропущено"), ("cancelled", "Отменено"),
    ])

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(fields=["assignment"], condition=models.Q(state__in=["pending", "open"]),
                                    name="one_live_queue_item_per_assignment"),
            models.CheckConstraint(condition=(
                models.Q(state="pending", attempt__isnull=True, finished_at__isnull=True) |
                models.Q(state="open", attempt__isnull=False, finished_at__isnull=True) |
                models.Q(state="done", attempt__isnull=False, finished_at__isnull=False) |
                models.Q(state="skipped", attempt__isnull=True, finished_at__isnull=False) |
                models.Q(state="cancelled", finished_at__isnull=False)
            ), name="queue_consistent_state"),
        ]


class DailySchedule(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    delivery_time = models.TimeField("Время выдачи", default=time(18, 0),
                                    help_text="Ежедневно, по московскому времени (Europe/Moscow).")

    class Meta:
        verbose_name = "Настройка ежедневной выдачи"
        verbose_name_plural = "Настройка ежедневной выдачи"
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="daily_schedule_singleton")]

    def __str__(self):
        return f"Ежедневная выдача в {self.delivery_time:%H:%M} (Москва)"


class DailyReviewRun(models.Model):
    student = models.ForeignKey("users.Student", on_delete=models.PROTECT)
    local_date = models.DateField()
    formed_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["student", "local_date"], name="one_daily_run_per_student")]
