from pathlib import Path

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import FileResponse, HttpResponseNotFound
from django.shortcuts import get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html

from .models import Course, Task, TaskAttachment, Topic


class AttachmentWidget(forms.ClearableFileInput):
    # The current file is linked separately through the permission-checked route.
    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        if value and getattr(value, "name", None):
            context["widget"]["value"] = "Текущее вложение"
            context["widget"]["is_initial"] = True
        return context

    template_name = "materials/attachment_widget.html"


class AttachmentForm(forms.ModelForm):
    class Meta:
        model = TaskAttachment
        fields = ("file",)
        widgets = {"file": AttachmentWidget()}


class AttachmentInline(admin.TabularInline):
    model = TaskAttachment
    form = AttachmentForm
    extra = 1
    fields = ("file", "download_link")
    readonly_fields = ("download_link",)

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("materials.view_task")

    def has_add_permission(self, request, obj=None):
        permission = "change_task" if obj else "add_task"
        return request.user.has_perm(f"materials.{permission}")

    def has_change_permission(self, request, obj=None):
        return request.user.has_perm("materials.change_task")

    def has_delete_permission(self, request, obj=None):
        return request.user.has_perm("materials.change_task")

    @admin.display(description="Скачать")
    def download_link(self, obj):
        if not obj or not obj.pk:
            return "Сначала сохраните задание"
        return format_html('<a href="{}">Скачать вложение: {}</a>',
                           reverse("admin:materials_task_attachment", args=[obj.task_id, obj.pk]),
                           Path(obj.file.name).name)


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("title",)
    search_fields = ("title",)


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ("title", "course", "order")
    list_filter = ("course",)
    list_select_related = ("course",)


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("title", "topic", "course", "is_active", "order")
    list_filter = ("topic__course", "topic", "is_active")
    search_fields = ("title", "question")
    list_select_related = ("topic__course",)
    inlines = (AttachmentInline,)
    readonly_fields = ("created_at", "updated_at")
    fields = ("topic", "title", "question", "answer",
              "is_active", "order", "created_at", "updated_at")
    actions = ("deactivate",)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "topic":
            kwargs["queryset"] = Topic.objects.select_related("course")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @admin.display(description="Курс", ordering="topic__course__title")
    def course(self, obj):
        return obj.topic.course

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Деактивировать выбранные задания", permissions=["change"])
    def deactivate(self, request, queryset):
        with transaction.atomic():
            for task in queryset:
                task.is_active = False
                task.save()
                self.log_change(request, task, "Задание деактивировано")
        self.message_user(request, "Выбранные задания деактивированы.", messages.SUCCESS)

    def get_urls(self):
        return [path("<int:task_id>/attachments/<int:attachment_id>/", self.admin_site.admin_view(self.download_attachment),
                     name="materials_task_attachment")] + super().get_urls()

    def download_attachment(self, request, task_id, attachment_id):
        if not request.user.has_perm("materials.view_task"):
            raise PermissionDenied
        task = get_object_or_404(Task, pk=task_id)
        attachment = get_object_or_404(TaskAttachment, pk=attachment_id, task=task)
        try:
            file = attachment.file.open("rb")
        except FileNotFoundError:
            return HttpResponseNotFound("Файл вложения не найден.", content_type="text/plain; charset=utf-8")
        return FileResponse(file, as_attachment=True, filename=Path(attachment.file.name).name)
