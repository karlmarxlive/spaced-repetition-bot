from zoneinfo import ZoneInfo

from django.db import migrations


def populate(apps, schema_editor):
    alias = schema_editor.connection.alias
    assignments = apps.get_model("users", "StudentTopic")
    progress = apps.get_model("repetitions", "TopicProgress")
    for assignment in assignments.objects.using(alias).all().iterator():
        progress.objects.using(alias).create(
            assignment_id=assignment.pk, interval_step=0,
            next_review_date=assignment.activated_at.astimezone(ZoneInfo("Europe/Moscow")).date(),
        )


class Migration(migrations.Migration):
    dependencies = [("repetitions", "0001_initial")]
    operations = [migrations.RunPython(populate, migrations.RunPython.noop)]
