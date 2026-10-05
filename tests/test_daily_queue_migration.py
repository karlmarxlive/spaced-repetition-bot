from datetime import timedelta
from importlib import import_module
from unittest.mock import patch

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from modules.study_sessions.models import Attempt, DailySchedule, ReviewQueueItem
from modules.study_sessions.services import accept_answer, continue_review, form_daily_review
from tests.test_daily_reviews import NOW


class DailyQueueMigrationTests(TransactionTestCase):
    def test_upgrade_preserves_history_snapshots_and_open_session(self):
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes()
        old_targets = [('study_sessions', '0002_relationship_guards'), ('repetitions', '0003_six_intervals')]
        try:
            executor.migrate(old_targets)
            apps = executor.loader.project_state(old_targets).apps
            Course = apps.get_model('materials', 'Course')
            Topic = apps.get_model('materials', 'Topic')
            Task = apps.get_model('materials', 'Task')
            Student = apps.get_model('users', 'Student')
            Assignment = apps.get_model('users', 'StudentTopic')
            Progress = apps.get_model('repetitions', 'TopicProgress')
            Session = apps.get_model('study_sessions', 'StudySession')
            OldAttempt = apps.get_model('study_sessions', 'Attempt')
            Cursor = apps.get_model('study_sessions', 'TaskCursor')
            course = Course.objects.create(title='Historical')
            student = Student.objects.create(course=course, telegram_id=123, first_name='Historical')
            topics = [Topic.objects.create(course=course, title=f'Topic {i}', order=i) for i in range(4)]
            tasks = [Task.objects.create(topic=t, title=t.title, question=f'Changed question {i}',
                                         answer='changed reference', is_active=True) for i, t in enumerate(topics)]
            assignments = [Assignment.objects.create(student=student, topic=t, activated_at=NOW) for t in topics]
            for i, assignment in enumerate(assignments):
                Progress.objects.create(assignment=assignment, interval_step=1 if i == 0 else 0,
                                        next_review_date=(NOW + timedelta(days=1 if i == 0 else 0)).date())
            session = Session.objects.create(student=student, started_at=NOW - timedelta(days=1),
                                             skipped=[{'assignment_id': assignments[2].pk, 'topic': 'Topic 2'}])
            old_attempts = []
            for i, state in enumerate(['correct', 'open']):
                old_attempts.append(OldAttempt.objects.create(
                    session=session, student=student, assignment=assignments[i], task=tasks[i],
                    opened_at=NOW - timedelta(days=1), status=state,
                    finished_at=NOW if state == 'correct' else None,
                    response='saved answer' if state == 'correct' else None,
                    question='Original question', reference='original reference', topic_title='Original topic',
                    attachments=[{'path': 'tasks/retained.txt', 'name': 'retained.txt'}], task_order=9))
            Cursor.objects.create(assignment=assignments[0], last_task=tasks[0], last_order=9)
            models = [Course, Topic, Task, Student, Assignment, Progress, Session, OldAttempt, Cursor]
            snapshots = {m._meta.label: list(m.objects.order_by('pk').values()) for m in models}

            migration = import_module('modules.study_sessions.migrations.0003_daily_queue')
            with patch.object(migration.timezone, 'now', return_value=NOW):
                executor = MigrationExecutor(connection)
                executor.migrate(leaves)
            current_apps = executor.loader.project_state(leaves).apps
            for label, expected in snapshots.items():
                self.assertEqual(list(current_apps.get_model(label).objects.order_by('pk').values()), expected, label)
            self.assertEqual(str(DailySchedule.objects.get().delivery_time), '18:00:00')
            self.assertEqual(ReviewQueueItem.objects.get(attempt_id=old_attempts[0].pk).state, 'done')
            self.assertEqual(ReviewQueueItem.objects.get(attempt_id=old_attempts[1].pk).state, 'open')
            self.assertEqual(list(ReviewQueueItem.objects.filter(state='pending').values_list('assignment_id', flat=True)),
                             [assignments[3].pk])
            call_count = ReviewQueueItem.objects.count()
            MigrationExecutor(connection).migrate(leaves)
            self.assertEqual(ReviewQueueItem.objects.count(), call_count)

            self.assertEqual(accept_answer(student.pk, old_attempts[1].pk, 'original reference', now=NOW).status, 'correct')
            next_question = continue_review(student.pk, now=NOW).question
            self.assertEqual(next_question.text, 'Changed question 3')
            tomorrow = NOW + timedelta(days=1)
            self.assertEqual(form_daily_review(student.pk, now=tomorrow).status, 'idle')
            accept_answer(student.pk, next_question.attempt_id, 'changed reference', now=tomorrow)
            retried_skip = continue_review(student.pk, now=tomorrow).question
            self.assertEqual(Attempt.objects.get(pk=retried_skip.attempt_id).assignment_id, assignments[2].pk)
            accept_answer(student.pk, retried_skip.attempt_id, 'changed reference', now=tomorrow)
            repeated = continue_review(student.pk, now=tomorrow).question
            self.assertEqual(Attempt.objects.get(pk=repeated.attempt_id).assignment_id, assignments[0].pk)
            # The former unique(session, assignment) must no longer exist.
            self.assertEqual(Attempt.objects.filter(session_id=session.pk, assignment_id=assignments[0].pk).count(), 2)
            with self.assertRaisesRegex(RuntimeError, 'restore a backup'):
                MigrationExecutor(connection).migrate(old_targets)
            self.assertEqual(ReviewQueueItem.objects.get(attempt_id=repeated.attempt_id).state, 'open')
        finally:
            MigrationExecutor(connection).migrate(leaves)
