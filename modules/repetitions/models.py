from django.core.validators import MaxValueValidator
from django.db import models

from config.repetitions import MAX_STEP


class TopicProgress(models.Model):
    assignment = models.OneToOneField(
        "users.StudentTopic", on_delete=models.PROTECT, related_name="progress",
    )
    interval_step = models.PositiveSmallIntegerField(default=0, validators=[MaxValueValidator(MAX_STEP)])
    next_review_date = models.DateField()

    class Meta:
        constraints = [models.CheckConstraint(
            condition=models.Q(interval_step__gte=0, interval_step__lte=MAX_STEP),
            name="progress_step_0_to_6",
        )]
