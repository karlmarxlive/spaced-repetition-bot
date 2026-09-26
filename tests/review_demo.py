"""Offline stage-5 demonstration and stage-4 upgrade on disposable SQLite."""
import asyncio
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.db import IntegrityError, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from modules.delivery.services import send_result, send_review
from modules.materials.models import Course, Task, TaskAttachment, Topic
from modules.repetitions.models import TopicProgress
from modules.study_sessions.services import accept_answer, start_review
from modules.users.models import Student, StudentTopic
from modules.users.services import assign_topics
from tests.test_repetitions import NOW


class ConsoleTransport:
    async def answer(self, text, **kwargs):
        print('BOT:', text)

    async def answer_document(self, document, **kwargs):
        assert Path(document.path).is_file()
        print('FILE:', document.filename)


def verify_upgrade(connection):
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    executor.migrate([('study_sessions', None), ('repetitions', '0002_existing_assignments')])
    executor = MigrationExecutor(connection)
    state = executor.loader.project_state([('repetitions', '0002_existing_assignments'),
                                          ('materials', '0002_remove_task_attachment_taskattachment')]).apps
    course = state.get_model('materials', 'Course').objects.create(title='Preserved')
    topic = state.get_model('materials', 'Topic').objects.create(course=course, title='Preserved')
    task = state.get_model('materials', 'Task').objects.create(topic=topic, title='Preserved')
    state.get_model('materials', 'TaskAttachment').objects.create(task=task, file='tasks/preserved/same.txt')
    student = state.get_model('users', 'Student').objects.create(course=course, telegram_id=100, first_name='Old')
    assignment = state.get_model('users', 'StudentTopic').objects.create(student=student, topic=topic,
                                                                       activated_at=NOW, is_active=False)
    state.get_model('repetitions', 'TopicProgress').objects.create(assignment=assignment, interval_step=5,
                                                                 next_review_date=date(2027, 1, 14))
    labels = ['materials.Course', 'materials.Topic', 'materials.Task', 'materials.TaskAttachment',
              'users.Student', 'users.StudentTopic', 'repetitions.TopicProgress']
    saved = {label: list(state.get_model(label).objects.order_by('pk').values()) for label in labels}
    MigrationExecutor(connection).migrate(leaves)
    current = MigrationExecutor(connection).loader.project_state(leaves).apps
    for label, rows in saved.items():
        assert list(current.get_model(label).objects.order_by('pk').values()) == rows, label
    TopicProgress.objects.filter(assignment_id=assignment.pk).update(interval_step=6)
    try:
        with transaction.atomic():
            TopicProgress.objects.filter(assignment_id=assignment.pk).update(interval_step=7)
    except IntegrityError:
        pass
    else:
        raise AssertionError('Database accepted step 7')
    call_command('migrate', verbosity=0)
    call_command('migrate', check=True, verbosity=0)
    progress = TopicProgress.objects.get(assignment_id=assignment.pk)
    assert (progress.interval_step, progress.next_review_date) == (6, date(2027, 1, 14))
    print('Stage 4 upgrade: fixtures and paths preserved; step 6 persists, step 7 rejected; repeat migrate OK')


def main():
    connections.close_all()
    connection = connections['default']
    original_name = connection.settings_dict['NAME']
    with TemporaryDirectory(prefix='review-demo-') as directory:
        connection.settings_dict['NAME'] = str(Path(directory) / 'isolated.sqlite3')
        try:
            with override_settings(MEDIA_ROOT=str(Path(directory) / 'media')):
                call_command('migrate', verbosity=0)
                call_command('migrate', check=True, verbosity=0)
                print('Empty temporary database: migrate / migrate --check OK')
                verify_upgrade(connection)
                course = Course.objects.create(title='Demo')
                topics = [Topic.objects.create(course=course, title=f'Topic {i}', order=i) for i in (1, 2)]
                student = Student.objects.create(course=course, telegram_id=42, first_name='Demo')
                for i, topic in enumerate(topics):
                    task = Task.objects.create(topic=topic, title='Demo', question=f'Question {i + 1}',
                                               answer='private-reference', is_active=True)
                    for j in range(2):
                        relative = f'tasks/{i}/{j}/same.txt'
                        path = Path(directory) / 'media' / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(f'Demo attachment {j}', encoding='utf-8')
                        TaskAttachment.objects.create(task=task, file=relative)
                assign_topics(student.pk, [t.pk for t in topics], now=NOW)
                transport = ConsoleTransport()
                review = start_review(student.pk, now=NOW)
                asyncio.run(send_review(transport, review))
                repeated = start_review(student.pk, now=NOW)
                assert repeated.question == review.question
                print('Repeated /review: same snapshot and attempt')
                for response in ('private-reference', 'wrong'):
                    result = accept_answer(student.pk, review.question.attempt_id, response, now=NOW)
                    asyncio.run(send_result(transport, result))
                    review = start_review(student.pk, now=NOW)
                    asyncio.run(send_review(transport, review))
                assert (review.summary.correct, review.summary.incorrect) == (1, 1)
                assert list(TopicProgress.objects.filter(assignment__student=student).order_by('assignment__topic__order')
                            .values_list('interval_step', 'next_review_date')) == [
                                (1, date(2026, 9, 22)), (0, date(2026, 9, 22))]
                print('Offline full lesson: OK (no Telegram API)')
        finally:
            connections.close_all()
            connection.settings_dict['NAME'] = original_name


if __name__ == '__main__':
    main()
