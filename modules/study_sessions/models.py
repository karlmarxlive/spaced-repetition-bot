from django.db import models


class StudySession(models.Model):
    student = models.ForeignKey("users.Student", on_delete=models.PROTECT)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
    state = models.CharField(max_length=10, default="open")
    # Manual traversal only; not a scheduled delivery queue.
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
            models.UniqueConstraint(fields=["session", "assignment"], name="one_topic_per_session"),
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
