"""SQLite cross-table invariants which cannot be expressed by a Django CHECK.

The project intentionally targets SQLite. Revisit these guards before switching DBs.
Task.topic is deliberately not checked on update: moving a task preserves history.
"""
from django.db import migrations


def guard(name, table, event, condition):
    return migrations.RunSQL(
        f"CREATE TRIGGER {name} BEFORE {event} ON {table} WHEN {condition} "
        "BEGIN SELECT RAISE(ABORT, 'inconsistent study relationship'); END;",
        f"DROP TRIGGER {name};",
    )


OWNER_MISMATCH = """
    NEW.student_id != (SELECT student_id FROM study_sessions_studysession WHERE id=NEW.session_id)
    OR NEW.student_id != (SELECT student_id FROM users_studenttopic WHERE id=NEW.assignment_id)
    OR (NEW.status='open' AND
        (SELECT state FROM study_sessions_studysession WHERE id=NEW.session_id) != 'open')
"""


class Migration(migrations.Migration):
    dependencies = [('study_sessions', '0001_initial')]
    operations = [
        guard('attempt_insert_relationships', 'study_sessions_attempt', 'INSERT', OWNER_MISMATCH + """
              OR (SELECT topic_id FROM materials_task WHERE id=NEW.task_id) !=
                 (SELECT topic_id FROM users_studenttopic WHERE id=NEW.assignment_id)
              """),
        guard('attempt_update_relationships', 'study_sessions_attempt', 'UPDATE', OWNER_MISMATCH + """
              OR NEW.student_id != OLD.student_id OR NEW.session_id != OLD.session_id
              OR NEW.assignment_id != OLD.assignment_id OR NEW.task_id != OLD.task_id
              """),
        guard('session_update_relationships', 'study_sessions_studysession', 'UPDATE', """
              (NEW.student_id != OLD.student_id AND
               EXISTS(SELECT 1 FROM study_sessions_attempt WHERE session_id=OLD.id))
              OR (NEW.state != 'open' AND
                  EXISTS(SELECT 1 FROM study_sessions_attempt WHERE session_id=OLD.id AND status='open'))
              """),
        guard('assignment_history_relationships', 'users_studenttopic', 'UPDATE', """
              (NEW.student_id != OLD.student_id OR NEW.topic_id != OLD.topic_id) AND
              EXISTS(SELECT 1 FROM study_sessions_attempt WHERE assignment_id=OLD.id)
              """),
    ]
