from datetime import timedelta
from pathlib import Path
import os
import subprocess
import sys
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandObject
from aiogram.types import Message, Update
from asgiref.sync import sync_to_async
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from bot.handlers import REPLIES, create_router, start
from modules.materials.models import Course, Topic
from modules.users.admin import InvitationAdmin
from modules.users.models import Invitation, Student, StudentTopic
from modules.users.services import assign_topics, register_student, revoke_invitation


class RegistrationTests(TestCase):
    def setUp(self):
        self.course = Course.objects.create(title="Информатика")
        self.invitation = Invitation.objects.create(course=self.course)

    def register(self, **kwargs):
        return register_student(**{"telegram_id": 2**40, "first_name": "Имя", "last_name": "Фамилия",
                                  "username": "student", "payload": self.invitation.token, **kwargs})

    def test_success_and_no_auth_account(self):
        count = get_user_model().objects.count()
        result = self.register()
        self.assertEqual(result.status, "registered")
        student = Student.objects.get(pk=result.student_id)
        self.assertEqual(student.telegram_id, 2**40)
        self.assertEqual(student.course, self.course)
        self.assertTrue(timezone.is_aware(student.registered_at))
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.student, student)
        self.assertIsNotNone(self.invitation.used_at)
        self.assertEqual(self.invitation.status, "Использовано")
        self.assertEqual(get_user_model().objects.count(), count)
        self.assertFalse(hasattr(student, "password"))
        self.assertFalse(hasattr(student, "is_staff"))
        self.assertFalse(hasattr(student, "is_superuser"))

    def test_no_invitation_and_malformed_payload(self):
        for payload, status in [(None, "needs_invitation"), ("", "needs_invitation"),
                                ("unknown", "unavailable"), ("a" * 65, "unavailable"),
                                ("bad payload", "unavailable"), ("токен", "unavailable"), ("a/b", "unavailable")]:
            with self.subTest(payload=payload):
                self.assertEqual(self.register(payload=payload).status, status)
        self.assertFalse(Student.objects.exists())
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.used_at)

    def test_revoked_invitation(self):
        self.assertTrue(revoke_invitation(self.invitation.pk))
        self.assertFalse(revoke_invitation(self.invitation.pk))
        self.assertEqual(self.register().status, "unavailable")
        self.assertFalse(Student.objects.exists())

    def test_used_invitation_does_not_register_other_account(self):
        self.register()
        self.assertEqual(self.register(telegram_id=99).status, "unavailable")
        self.assertEqual(Student.objects.count(), 1)
        self.assertFalse(revoke_invitation(self.invitation.pk))

    def test_repeated_event_preserves_identity_course_topics_and_teacher_name(self):
        first = self.register()
        student = Student.objects.get(pk=first.student_id)
        student.display_name = "Имя учителя"
        student.save()
        topic = Topic.objects.create(course=self.course, title="Логика")
        assign_topics(student.pk, [topic.pk])
        other = Invitation.objects.create(course=Course.objects.create(title="Другой курс"))
        for payload in (self.invitation.token, other.token, None, "invalid payload"):
            result = self.register(payload=payload, first_name="Новое", last_name=None, username=None)
            self.assertEqual(result.status, "existing")
            self.assertEqual(result.student_id, student.pk)
        student.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(Student.objects.count(), 1)
        self.assertEqual(student.first_name, "Новое")
        self.assertEqual(student.last_name, "")
        self.assertEqual(student.username, "")
        self.assertEqual(student.display_name, "Имя учителя")
        self.assertEqual(student.course, self.course)
        self.assertTrue(student.topic_assignments.get().is_active)
        self.assertIsNone(other.used_at)
        self.register(username="new_username")
        student.refresh_from_db()
        self.assertEqual(student.username, "new_username")

    def test_database_identity_constraints(self):
        self.register()
        for telegram_id in (2**40, 0, -1):
            with self.subTest(telegram_id=telegram_id), self.assertRaises(IntegrityError), transaction.atomic():
                Student.objects.create(telegram_id=telegram_id, first_name="Test", course=self.course)

    def test_invalid_identity(self):
        for telegram_id in (0, -1, 2**63, True, "12"):
            self.assertEqual(self.register(telegram_id=telegram_id).status, "invalid")
        self.assertFalse(Student.objects.exists())

    def test_atomic_rollback_after_invitation_update(self):
        from django.db.models.query import QuerySet
        original = QuerySet.update

        def fail_after_write(queryset, **kwargs):
            result = original(queryset, **kwargs)
            if queryset.model is Invitation:
                raise RuntimeError("simulated failure after claim")
            return result

        with patch.object(QuerySet, "update", fail_after_write), self.assertRaises(RuntimeError):
            self.register()
        self.assertFalse(Student.objects.exists())
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.used_at)
        self.assertIsNone(self.invitation.student_id)
        self.assertEqual(self.register().status, "registered")

    def test_conditional_claim_failure_rolls_back_student(self):
        from django.db.models.query import QuerySet
        original = QuerySet.update

        def conflict(queryset, **kwargs):
            return 0 if queryset.model is Invitation else original(queryset, **kwargs)

        with patch.object(QuerySet, "update", conflict):
            self.assertEqual(self.register().status, "unavailable")
        self.assertFalse(Student.objects.exists())

    def test_lock_is_controlled_without_losing_invitation(self):
        with patch("modules.users.services._register", side_effect=OperationalError("database is locked")):
            self.assertEqual(self.register().status, "busy")
        self.assertEqual(self.register().status, "registered")

    def test_tokens_are_random_url_safe_and_not_in_labels(self):
        other = Invitation.objects.create(course=self.course)
        self.assertNotEqual(other.token, self.invitation.token)
        self.assertRegex(other.token, r"^[A-Za-z0-9_-]{43}$")
        self.assertNotIn(other.token, str(other))


class AssignmentTests(TestCase):
    def setUp(self):
        self.course = Course.objects.create(title="Course")
        self.topic = Topic.objects.create(course=self.course, title="Без заданий")
        self.foreign = Topic.objects.create(course=Course.objects.create(title="Other"), title="Other")
        self.student = Student.objects.create(telegram_id=1, first_name="Test", course=self.course)

    def test_add_disable_reenable_unchanged_and_independent_students(self):
        other = Student.objects.create(telegram_id=2, first_name="Other", course=self.course)
        assign_topics(self.student.pk, [self.topic.pk])
        row = self.student.topic_assignments.get()
        first_time = row.activated_at
        assign_topics(self.student.pk, [self.topic.pk])
        row.refresh_from_db()
        self.assertEqual(row.activated_at, first_time)
        assign_topics(other.pk, [self.topic.pk])
        assign_topics(self.student.pk, [])
        row.refresh_from_db()
        self.assertFalse(row.is_active)
        self.assertEqual(row.activated_at, first_time)
        self.assertTrue(other.topic_assignments.get().is_active)
        with patch("modules.users.services.timezone.now", return_value=first_time + timedelta(days=1)):
            assign_topics(self.student.pk, [self.topic.pk])
        row.refresh_from_db()
        self.assertTrue(row.is_active)
        self.assertEqual(row.activated_at, first_time + timedelta(days=1))
        self.assertEqual(self.student.topic_assignments.get().pk, row.pk)

    def test_wrong_course_and_unknown_topic_are_rejected_atomically(self):
        assign_topics(self.student.pk, [self.topic.pk])
        for ids in ([self.foreign.pk], [999999], [self.topic.pk, self.foreign.pk]):
            with self.assertRaises(ValidationError):
                assign_topics(self.student.pk, ids)
        self.assertTrue(self.student.topic_assignments.get().is_active)
        with self.assertRaises(ValidationError):
            StudentTopic(student=self.student, topic=self.foreign, activated_at=timezone.now()).full_clean()

    def test_pair_unique_in_database(self):
        assign_topics(self.student.pk, [self.topic.pk])
        with self.assertRaises(IntegrityError), transaction.atomic():
            StudentTopic.objects.create(student=self.student, topic=self.topic, activated_at=timezone.now())


class UsersAdminTests(TestCase):
    def setUp(self):
        self.teacher = get_user_model().objects.create_superuser("teacher", password="test")
        self.client.force_login(self.teacher)
        self.course = Course.objects.create(title="Информатика")
        self.topic = Topic.objects.create(course=self.course, title="Логика")
        self.foreign = Topic.objects.create(course=Course.objects.create(title="Другой"), title="Чужая")
        self.student = Student.objects.create(telegram_id=100, first_name="Пётр", course=self.course)
        self.url = reverse("admin:users_student_change", args=[self.student.pk])

    @override_settings(TELEGRAM_BOT_USERNAME="ExampleBot")
    def test_create_link_revoke_and_no_token_in_audit_log(self):
        url = reverse("admin:users_invitation_add")
        self.assertEqual(self.client.post(url, {"course": self.course.pk, "note": "Для ученика", "_save": "1"}).status_code, 302)
        invitation = Invitation.objects.get()
        change_url = reverse("admin:users_invitation_change", args=[invitation.pk])
        response = self.client.get(change_url)
        self.assertContains(response, f"https://t.me/ExampleBot?start={invitation.token}")
        self.assertContains(response, "Доступно")
        for entry in LogEntry.objects.all():
            self.assertNotIn(invitation.token, entry.object_repr + entry.change_message)
        self.client.post(reverse("admin:users_invitation_changelist"), {
            "action": "revoke", "_selected_action": [invitation.pk],
        })
        invitation.refresh_from_db()
        self.assertTrue(invitation.is_revoked)
        self.assertContains(self.client.get(change_url), "Отозвано")
        self.client.post(change_url, {"course": self.foreign.course_id, "note": "changed", "is_revoked": "",
                                      "token": "forged", "_save": "1"})
        invitation.refresh_from_db()
        self.assertTrue(invitation.is_revoked)
        self.assertEqual(invitation.course_id, self.course.pk)
        self.assertNotEqual(invitation.token, "forged")

    def test_missing_or_invalid_bot_username(self):
        invitation = Invitation.objects.create(course=self.course)
        model_admin = InvitationAdmin(Invitation, admin.site)
        for username in ("", "@ExampleBot", "bad/name"):
            with override_settings(TELEGRAM_BOT_USERNAME=username):
                text = model_admin.link(invitation)
                self.assertIn("TELEGRAM_BOT_USERNAME", text)
                self.assertNotIn("https://", text)

    def test_used_invitation_cannot_be_reset(self):
        invitation = Invitation.objects.create(course=self.course)
        result = register_student(telegram_id=200, first_name="Other", payload=invitation.token)
        url = reverse("admin:users_invitation_change", args=[invitation.pk])
        self.client.post(url, {"note": "changed", "used_at": "", "student": "", "is_revoked": "", "_save": "1"})
        self.client.post(reverse("admin:users_invitation_changelist"), {
            "action": "revoke", "_selected_action": [invitation.pk],
        })
        invitation.refresh_from_db()
        self.assertEqual(invitation.student_id, result.student_id)
        self.assertIsNotNone(invitation.used_at)
        self.assertFalse(invitation.is_revoked)

    def test_student_form_assignments_and_immutable_fields(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Логика")
        self.assertNotContains(response, "Чужая")
        self.assertEqual(self.client.post(self.url, {"display_name": "Петя", "completed_topics": [self.topic.pk],
            "telegram_id": 999, "course": self.foreign.course_id, "first_name": "Forged", "_save": "1"}).status_code, 302)
        self.student.refresh_from_db()
        self.assertEqual(self.student.display_name, "Петя")
        self.assertEqual(self.student.first_name, "Пётр")
        self.assertEqual(self.student.telegram_id, 100)
        self.assertEqual(self.student.course_id, self.course.pk)
        row = self.student.topic_assignments.get()
        original_time = row.activated_at
        self.client.post(self.url, {"display_name": "Петя", "completed_topics": [self.topic.pk], "_save": "1"})
        row.refresh_from_db()
        self.assertEqual(row.activated_at, original_time)
        self.client.post(self.url, {"display_name": "Петя", "_save": "1"})
        row.refresh_from_db()
        self.assertFalse(row.is_active)
        self.client.post(self.url, {"display_name": "Петя", "completed_topics": [self.topic.pk], "_save": "1"})
        row.refresh_from_db()
        self.assertTrue(row.is_active)
        self.assertGreater(row.activated_at, original_time)

    def test_forged_foreign_topic_rejected_without_partial_save(self):
        response = self.client.post(self.url, {"display_name": "Forged", "completed_topics": [self.foreign.pk], "_save": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("completed_topics", response.context["adminform"].form.errors)
        self.student.refresh_from_db()
        self.assertEqual(self.student.display_name, "")
        self.assertFalse(self.student.topic_assignments.exists())

    def test_no_manual_student_creation_deletion_or_low_level_editor(self):
        self.assertEqual(self.client.post(reverse("admin:users_student_add"), {}).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:users_student_delete", args=[self.student.pk]), {"post": "yes"}).status_code, 403)
        self.assertNotIn(StudentTopic, admin.site._registry)
        response = self.client.get(reverse("admin:users_student_changelist"))
        self.assertNotContains(response, 'value="delete_selected"')
        self.assertContains(response, "Пётр")
        for query in ("Пётр", "100"):
            self.assertContains(self.client.get(reverse("admin:users_student_changelist"), {"q": query}), "Пётр")


class StartIntegrationTests(TransactionTestCase):
    def setUp(self):
        self.course = Course.objects.create(title="Course")
        self.invitation = Invitation.objects.create(course=self.course)

    async def dispatch(self, text, *, chat_type="private", user=None):
        bot = Bot("123456789:" + "a" * 35)
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router())
        update = Update.model_validate({"update_id": 1, "message": {
            "message_id": 1, "date": 0, "chat": {"id": 42, "type": chat_type},
            "from": user or {"id": 42, "first_name": "From Telegram", "is_bot": False},
            "text": text, "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        }})
        try:
            with patch.object(bot.session, "make_request", new_callable=AsyncMock) as transport:
                await dispatcher.feed_update(bot, update)
            transport.assert_awaited_once()
            return transport.call_args.args[1].text
        finally:
            await bot.session.close()

    async def test_registration_repeat_and_update_through_dispatcher(self):
        text = f"/start {self.invitation.token}"
        self.assertEqual(await self.dispatch(text), REPLIES["registered"])
        self.assertEqual(await self.dispatch(text), REPLIES["existing"])
        self.assertEqual(await self.dispatch("/start", user={"id": 42, "first_name": "Changed", "username": "changed",
                                                          "is_bot": False}), REPLIES["existing"])
        student = await sync_to_async(Student.objects.get, thread_sensitive=True)(telegram_id=42)
        self.assertEqual(student.first_name, "Changed")
        self.assertEqual(student.username, "changed")
        self.assertEqual(await sync_to_async(Student.objects.count, thread_sensitive=True)(), 1)

    async def test_groups_and_bot_accounts_do_not_consume_invitation(self):
        for chat_type in ("group", "supergroup"):
            self.assertIn("личном чате", await self.dispatch(f"/start {self.invitation.token}", chat_type=chat_type))
        self.assertEqual(await self.dispatch(f"/start {self.invitation.token}", user={
            "id": 42, "first_name": "Bot", "is_bot": True}), REPLIES["invalid"])
        self.assertFalse(await sync_to_async(Student.objects.exists, thread_sensitive=True)())
        await sync_to_async(self.invitation.refresh_from_db, thread_sensitive=True)()
        self.assertIsNone(self.invitation.used_at)

    async def test_missing_sender(self):
        message = Message.model_validate({"message_id": 1, "date": 0, "chat": {"id": 42, "type": "private"},
                                          "text": "/start"})
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            await start(message, CommandObject(prefix="/", command="start", args=self.invitation.token))
        answer.assert_awaited_once_with(REPLIES["invalid"])
        self.assertFalse(await sync_to_async(Student.objects.exists, thread_sensitive=True)())

    async def test_invalid_payload_through_dispatcher(self):
        for payload in ("bad payload", "a" * 65, "unknown"):
            self.assertEqual(await self.dispatch("/start " + payload), REPLIES["unavailable"])
        self.assertFalse(await sync_to_async(Student.objects.exists, thread_sensitive=True)())


class SQLiteConcurrencyTests(TestCase):
    def test_real_file_database_races_and_lock_recovery(self):
        result = subprocess.run([sys.executable, "-m", "tests.sqlite_concurrency"],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SQLite races and lock recovery: OK", result.stdout)
