"""Offline stage-6 demonstration: a disposable SQLite file and controlled time."""
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.db import connections

from modules.materials.models import Course, Task, Topic
from modules.study_sessions.models import Attempt, DailyReviewRun, ReviewQueueItem
from modules.study_sessions.services import accept_answer, continue_review, current_question, form_daily_review
from modules.users.models import Student
from modules.users.services import assign_topics
from tests.test_daily_reviews import NOW


def main():
    connections.close_all()
    with TemporaryDirectory(prefix='daily-reviews-demo-') as directory:
        connection = connections['default']
        connection.settings_dict['NAME'] = str(Path(directory) / 'demo.sqlite3')
        try:
            call_command('migrate', verbosity=0)
            course = Course.objects.create(title='Demo')
            student = Student.objects.create(course=course, telegram_id=42, first_name='Demo')
            topics = [Topic.objects.create(course=course, title=f'Topic {i}', order=i) for i in range(2)]
            for topic in topics:
                Task.objects.create(topic=topic, title=topic.title, question=topic.title, answer='yes', is_active=True)
            assign_topics(student.pk, [t.pk for t in topics], now=NOW)
            assert form_daily_review(student.pk, now=NOW - timedelta(seconds=1)).status == 'idle'
            print('17:59:59 Moscow: no delivery')
            first = form_daily_review(student.pk, now=NOW).question
            assert ReviewQueueItem.objects.count() == 2
            assert form_daily_review(student.pk, now=NOW).status == 'idle'
            print('18:00 Moscow: one question, second topic queued; duplicate tick is idle')
            accept_answer(student.pk, first.attempt_id, 'yes', now=NOW)
            second = continue_review(student.pk, now=NOW).question
            connections.close_all()
            assert current_question(student.pk) == second
            tomorrow = NOW + timedelta(days=1)
            assert form_daily_review(student.pk, now=tomorrow).status == 'idle'
            assert current_question(student.pk) == second
            assert ReviewQueueItem.objects.filter(state='pending').count() == 1
            print('Next day after reopening DB: same unanswered question, first topic queued again')
            accept_answer(student.pk, second.attempt_id, 'yes', now=tomorrow)
            repeated = continue_review(student.pk, now=tomorrow).question
            assert repeated.text == first.text and repeated.attempt_id != first.attempt_id
            accept_answer(student.pk, repeated.attempt_id, 'yes', now=tomorrow)
            assert continue_review(student.pk, now=tomorrow).summary.correct == 3
            assert Attempt.objects.count() == 3
            assert DailyReviewRun.objects.count() == 2
            call_command('migrate', check=True, verbosity=0)
            print('One multi-day session: 3 correct answers, 2 daily batches; preserved on disk')
            print('Stage 6 offline demo: OK')
        finally:
            connections.close_all()


if __name__ == '__main__':
    main()
