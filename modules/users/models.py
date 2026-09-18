import secrets

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models


def invitation_token():
    return secrets.token_urlsafe(32)


class Student(models.Model):
    telegram_id = models.PositiveBigIntegerField("Telegram ID", unique=True, validators=[MinValueValidator(1)])
    username = models.CharField("Username", max_length=32, blank=True)
    first_name = models.CharField("Имя Telegram", max_length=64)
    last_name = models.CharField("Фамилия Telegram", max_length=64, blank=True)
    display_name = models.CharField("Имя для учителя", max_length=200, blank=True)
    course = models.ForeignKey("materials.Course", on_delete=models.PROTECT, verbose_name="Курс")
    registered_at = models.DateTimeField("Дата регистрации", auto_now_add=True)

    class Meta:
        verbose_name = "Ученик"
        verbose_name_plural = "Ученики"
        ordering = ["-registered_at", "pk"]
        constraints = [models.CheckConstraint(condition=models.Q(telegram_id__gt=0), name="student_positive_telegram_id")]

    def __str__(self):
        return self.display_name or " ".join(filter(None, [self.first_name, self.last_name]))


class Invitation(models.Model):
    course = models.ForeignKey("materials.Course", on_delete=models.PROTECT, verbose_name="Курс")
    token = models.CharField("Токен", max_length=64, unique=True, default=invitation_token, editable=False)
    note = models.TextField("Заметка учителя", blank=True)
    created_at = models.DateTimeField("Дата создания", auto_now_add=True)
    is_revoked = models.BooleanField("Отозвано", default=False)
    used_at = models.DateTimeField("Дата использования", null=True, blank=True, editable=False)
    student = models.OneToOneField(Student, on_delete=models.PROTECT, null=True, blank=True,
                                  editable=False, verbose_name="Ученик")

    class Meta:
        verbose_name = "Приглашение"
        verbose_name_plural = "Приглашения"
        ordering = ["-created_at", "pk"]
        constraints = [models.CheckConstraint(
            condition=(models.Q(used_at__isnull=True, student__isnull=True) |
                       models.Q(used_at__isnull=False, student__isnull=False, is_revoked=False)),
            name="invitation_consistent_usage",
        )]

    @property
    def status(self):
        if self.used_at:
            return "Использовано"
        return "Отозвано" if self.is_revoked else "Доступно"

    def __str__(self):
        # Admin's audit log stores this label: never include the secret token.
        return f"Приглашение №{self.pk} — {self.status}"


class StudentTopic(models.Model):
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="topic_assignments", verbose_name="Ученик")
    topic = models.ForeignKey("materials.Topic", on_delete=models.PROTECT, verbose_name="Тема")
    is_active = models.BooleanField("Пройдена и включена", default=True)
    activated_at = models.DateTimeField("Последнее включение")

    class Meta:
        verbose_name = "Пройденная тема"
        verbose_name_plural = "Пройденные темы"
        constraints = [models.UniqueConstraint(fields=["student", "topic"], name="unique_student_topic")]

    def clean(self):
        super().clean()
        if self.student_id and self.topic_id and self.student.course_id != self.topic.course_id:
            raise ValidationError({"topic": "Можно назначить только тему курса ученика."})
