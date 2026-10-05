"""Persistent queue, daily settings and lossless migration of existing attempts.

SQLite remakes Attempt when removing its table-level unique constraint. Drop
cross-table triggers first, then restore them explicitly after the remake.
"""

import datetime
from importlib import import_module
from zoneinfo import ZoneInfo
import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


previous = import_module('modules.study_sessions.migrations.0002_relationship_guards')
old_guards = previous.Migration.operations


def populate_queue(apps, schema_editor):
    alias = schema_editor.connection.alias
    Schedule = apps.get_model('study_sessions', 'DailySchedule')
    Queue = apps.get_model('study_sessions', 'ReviewQueueItem')
    Attempt = apps.get_model('study_sessions', 'Attempt')
    Session = apps.get_model('study_sessions', 'StudySession')
    Assignment = apps.get_model('users', 'StudentTopic')
    Schedule.objects.using(alias).get_or_create(pk=1)
    for attempt in Attempt.objects.using(alias).order_by('pk').iterator():
        Queue.objects.using(alias).create(
            session_id=attempt.session_id, assignment_id=attempt.assignment_id,
            attempt_id=attempt.pk, enqueued_at=attempt.opened_at, finished_at=attempt.finished_at,
            state={'open': 'open', 'cancelled': 'cancelled'}.get(attempt.status, 'done'))
    now = timezone.now()
    today = now.astimezone(ZoneInfo('Europe/Moscow')).date()
    # Preserve the continuation that the old dynamic /review would have offered.
    for session in Session.objects.using(alias).filter(state='open').iterator():
        excluded = {item['assignment_id'] for item in session.skipped}
        excluded.update(Attempt.objects.using(alias).filter(session_id=session.pk).values_list('assignment_id', flat=True))
        assignments = Assignment.objects.using(alias).filter(
            student_id=session.student_id, is_active=True, progress__next_review_date__lte=today
        ).exclude(pk__in=excluded).order_by('progress__next_review_date', 'topic__order', 'topic_id')
        for assignment in assignments:
            Queue.objects.using(alias).create(session_id=session.pk, assignment_id=assignment.pk, enqueued_at=now)


def check_reverse(apps, schema_editor):
    Attempt = apps.get_model('study_sessions', 'Attempt')
    duplicates = Attempt.objects.using(schema_editor.connection.alias).values('session_id', 'assignment_id').annotate(
        count=models.Count('pk')).filter(count__gt=1)
    if duplicates.exists():
        raise RuntimeError('Stage 6 has repeated topics within sessions; restore a backup instead of reversing.')


QUEUE_RELATIONSHIP = """
    (SELECT student_id FROM study_sessions_studysession WHERE id=NEW.session_id) !=
    (SELECT student_id FROM users_studenttopic WHERE id=NEW.assignment_id)
    OR (NEW.state IN ('pending', 'open') AND
        (SELECT state FROM study_sessions_studysession WHERE id=NEW.session_id) != 'open')
    OR (NEW.attempt_id IS NOT NULL AND (
        NEW.session_id != (SELECT session_id FROM study_sessions_attempt WHERE id=NEW.attempt_id)
        OR NEW.assignment_id != (SELECT assignment_id FROM study_sessions_attempt WHERE id=NEW.attempt_id)))
"""


class Migration(migrations.Migration):

    dependencies = [
        ('study_sessions', '0002_relationship_guards'),
        ('users', '0001_initial'),
        ('repetitions', '0003_six_intervals'),
    ]

    operations = [
        migrations.RunSQL([item.reverse_sql for item in old_guards], [item.sql for item in old_guards]),
        migrations.CreateModel(
            name='DailyReviewRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('local_date', models.DateField()),
                ('formed_at', models.DateTimeField()),
            ],
        ),
        migrations.CreateModel(
            name='DailySchedule',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ('delivery_time', models.TimeField(default=datetime.time(18, 0), help_text='Ежедневно, по московскому времени (Europe/Moscow).', verbose_name='Время выдачи')),
            ],
            options={
                'verbose_name': 'Настройка ежедневной выдачи',
                'verbose_name_plural': 'Настройка ежедневной выдачи',
            },
        ),
        migrations.CreateModel(
            name='ReviewQueueItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('enqueued_at', models.DateTimeField()),
                ('finished_at', models.DateTimeField(null=True)),
                ('state', models.CharField(choices=[('pending', 'В очереди'), ('open', 'Открыто'), ('done', 'Завершено'), ('skipped', 'Пропущено'), ('cancelled', 'Отменено')], default='pending', max_length=10)),
            ],
            options={
                'ordering': ['pk'],
            },
        ),
        migrations.RemoveConstraint(
            model_name='attempt',
            name='one_topic_per_session',
        ),
        migrations.AddField(
            model_name='dailyreviewrun',
            name='student',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='users.student'),
        ),
        migrations.AddConstraint(
            model_name='dailyschedule',
            constraint=models.CheckConstraint(condition=models.Q(('id', 1)), name='daily_schedule_singleton'),
        ),
        migrations.AddField(
            model_name='reviewqueueitem',
            name='assignment',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='users.studenttopic'),
        ),
        migrations.AddField(
            model_name='reviewqueueitem',
            name='attempt',
            field=models.OneToOneField(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='queue_item', to='study_sessions.attempt'),
        ),
        migrations.AddField(
            model_name='reviewqueueitem',
            name='session',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='queue', to='study_sessions.studysession'),
        ),
        migrations.AddConstraint(
            model_name='dailyreviewrun',
            constraint=models.UniqueConstraint(fields=('student', 'local_date'), name='one_daily_run_per_student'),
        ),
        migrations.AddConstraint(
            model_name='reviewqueueitem',
            constraint=models.UniqueConstraint(condition=models.Q(('state__in', ['pending', 'open'])), fields=('assignment',), name='one_live_queue_item_per_assignment'),
        ),
        migrations.AddConstraint(
            model_name='reviewqueueitem',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('attempt__isnull', True), ('finished_at__isnull', True), ('state', 'pending')), models.Q(('attempt__isnull', False), ('finished_at__isnull', True), ('state', 'open')), models.Q(('attempt__isnull', False), ('finished_at__isnull', False), ('state', 'done')), models.Q(('attempt__isnull', True), ('finished_at__isnull', False), ('state', 'skipped')), models.Q(('finished_at__isnull', False), ('state', 'cancelled')), _connector='OR'), name='queue_consistent_state'),
        ),
        migrations.RunPython(populate_queue, migrations.RunPython.noop),
        migrations.RunSQL([item.sql for item in old_guards], [item.reverse_sql for item in old_guards]),
        previous.guard('queue_insert_relationships', 'study_sessions_reviewqueueitem', 'INSERT', QUEUE_RELATIONSHIP),
        previous.guard('queue_update_relationships', 'study_sessions_reviewqueueitem', 'UPDATE', QUEUE_RELATIONSHIP + """
            OR NEW.session_id != OLD.session_id OR NEW.assignment_id != OLD.assignment_id
            OR (OLD.attempt_id IS NOT NULL AND (NEW.attempt_id IS NULL OR NEW.attempt_id != OLD.attempt_id))
        """),
        previous.guard('queue_session_relationships', 'study_sessions_studysession', 'UPDATE', """
            (NEW.student_id != OLD.student_id AND
             EXISTS(SELECT 1 FROM study_sessions_reviewqueueitem WHERE session_id=OLD.id))
            OR (NEW.state != 'open' AND EXISTS(SELECT 1 FROM study_sessions_reviewqueueitem
                WHERE session_id=OLD.id AND state IN ('pending', 'open')))
        """),
        previous.guard('queue_assignment_relationships', 'users_studenttopic', 'UPDATE', """
            (NEW.student_id != OLD.student_id OR NEW.topic_id != OLD.topic_id) AND
            EXISTS(SELECT 1 FROM study_sessions_reviewqueueitem WHERE assignment_id=OLD.id)
        """),
        migrations.RunPython(migrations.RunPython.noop, check_reverse),

    ]
