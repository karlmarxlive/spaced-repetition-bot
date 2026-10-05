from django.contrib import admin

from modules.study_sessions.models import DailySchedule


@admin.register(DailySchedule)
class DailyScheduleAdmin(admin.ModelAdmin):
    fields = ["delivery_time"]
    list_display = ["__str__"]

    def has_add_permission(self, request):
        return super().has_add_permission(request) and not DailySchedule.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
