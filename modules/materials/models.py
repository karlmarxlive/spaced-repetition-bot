from pathlib import Path
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import models


def attachment_path(instance, filename):
    return f"tasks/{uuid4().hex}/{Path(filename).name}"


class Course(models.Model):
    title = models.CharField("Название", max_length=200)
    description = models.TextField("Описание", blank=True)

    class Meta:
        verbose_name = "Курс"
        verbose_name_plural = "Курсы"
        ordering = ["pk"]

    def __str__(self):
        return self.title


class Topic(models.Model):
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="topics", verbose_name="Курс")
    title = models.CharField("Название", max_length=200)
    description = models.TextField("Описание", blank=True)
    order = models.PositiveIntegerField("Порядок", default=0)

    class Meta:
        verbose_name = "Тема"
        verbose_name_plural = "Темы"
        ordering = ["order", "pk"]

    def __str__(self):
        return f"{self.course}: {self.title}"


class Task(models.Model):
    topic = models.ForeignKey(Topic, on_delete=models.PROTECT, related_name="tasks", verbose_name="Тема")
    title = models.CharField("Название для учителя", max_length=200)
    question = models.TextField("Условие", blank=True)
    answer = models.CharField("Эталон короткого ответа", max_length=500, blank=True)
    attachment = models.FileField("Вложение", upload_to=attachment_path, max_length=500, blank=True)
    is_active = models.BooleanField("Активно", default=False)
    order = models.PositiveIntegerField("Порядок", default=0)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Задание"
        verbose_name_plural = "Задания"
        ordering = ["order", "pk"]

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if self.is_active:
            errors = {}
            if not self.question or not self.question.strip():
                errors["question"] = "Для активации заполните текст условия."
            if not self.answer or not self.answer.strip():
                errors["answer"] = "Для активации заполните эталон ответа."
            if errors:
                raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
