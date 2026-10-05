import re

from aiogram.utils.deep_linking import create_deep_link
from django.urls import path, reverse
from modules.users import statistics
from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.utils.html import format_html
from django.utils import timezone

from modules.materials.models import Topic
from modules.users.models import Invitation, Student
from modules.users.services import assign_topics, revoke_invitation


class StudentForm(forms.ModelForm):
    completed_topics = forms.ModelMultipleChoiceField(
        label="Пройденные темы", queryset=Topic.objects.none(), required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Снятие отметки отключает тему и отменяет её очередь и открытое задание. Выдача ежедневная; /review запускает занятие вручную.",
    )

    class Meta:
        model = Student
        fields = ["display_name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["completed_topics"].queryset = Topic.objects.filter(
                course_id=self.instance.course_id).select_related("course")
            self.initial["completed_topics"] = list(self.instance.topic_assignments.filter(
                is_active=True).values_list("topic_id", flat=True))


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    form = StudentForm
    list_display = ["name", "telegram_id", "username", "course", "registered_at"]
    list_filter = ["course"]
    search_fields = ["display_name", "first_name", "last_name", "username", "=telegram_id"]
    readonly_fields = ["statistics_link", "telegram_id", "username", "first_name", "last_name", "course", "registered_at"]
    fields = ["display_name", *readonly_fields, "completed_topics"]
    list_select_related = ["course"]

    def get_urls(self):
        def view(function):
            return self.admin_site.admin_view(lambda request, **kwargs: function(self, request, **kwargs))
        return [
            path('<int:student_id>/statistics/', view(statistics.statistics), name='users_student_statistics'),
            path('<int:student_id>/statistics/<int:attempt_id>/', view(statistics.attempt_detail), name='users_student_attempt'),
            path('<int:student_id>/statistics/<int:attempt_id>/files/<int:index>/', view(statistics.attempt_file), name='users_student_attempt_file'),
        ] + super().get_urls()

    @admin.display(description='Учебная статистика')
    def statistics_link(self, obj):
        return format_html('<a href="{}">Прогресс, история и доставка</a>',
                           reverse('admin:users_student_statistics', args=[obj.pk]))

    @admin.display(description="Имя ученика")
    def name(self, obj):
        return str(obj)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        assign_topics(form.instance.pk, [topic.pk for topic in form.cleaned_data["completed_topics"]], now=timezone.now())


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ["id", "course", "state", "note", "created_at", "used_at", "student"]
    list_select_related = ["course", "student"]
    list_filter = ["course", "is_revoked"]
    readonly_fields = ["state", "link", "created_at", "used_at", "student"]
    fields = ["course", "note", *readonly_fields]
    actions = ["revoke"]

    def get_readonly_fields(self, request, obj=None):
        return [*self.readonly_fields, *(["course"] if obj else [])]

    @admin.display(description="Состояние")
    def state(self, obj):
        return obj.status

    @admin.display(description="Ссылка для ученика")
    def link(self, obj):
        username = settings.TELEGRAM_BOT_USERNAME
        if not username:
            return "Задайте TELEGRAM_BOT_USERNAME без @ в настройках и перезапустите панель."
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return "Проверьте TELEGRAM_BOT_USERNAME: имя без @, 5–32 латинских букв, цифр или _."
        if not obj.pk:
            return "Сохраните приглашение, чтобы получить ссылку."
        if obj.used_at or obj.is_revoked:
            return "Приглашение недоступно."
        url = create_deep_link(username, "start", obj.token)
        return format_html('<a href="{}">{}</a>', url, url)

    @admin.action(description="Отозвать выбранные неиспользованные приглашения", permissions=["change"])
    def revoke(self, request, queryset):
        count = sum(revoke_invitation(pk) for pk in queryset.values_list("pk", flat=True))
        self.message_user(request, f"Отозвано приглашений: {count}.", messages.SUCCESS)

    def has_delete_permission(self, request, obj=None):
        return False
