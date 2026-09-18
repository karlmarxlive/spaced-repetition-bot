import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase, override_settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse

from modules.materials.admin import TaskForm
from modules.materials.models import Course, Task, TaskAttachment, Topic


class MaterialsTests(TestCase):
    def setUp(self):
        self.course = Course.objects.create(title="Информатика")
        self.topic = Topic.objects.create(course=self.course, title="Кодирование")

    def test_hierarchy_and_draft(self):
        task = Task.objects.create(topic=self.topic, title="Черновик")
        self.assertFalse(task.is_active)
        self.assertEqual(task.topic.course, self.course)
        self.assertEqual(list(self.topic.tasks.all()), [task])
        self.assertIsNotNone(task.created_at)
        self.assertIsNotNone(task.updated_at)

    def test_stable_order(self):
        second = Topic.objects.create(course=self.course, title="Вторая", order=0)
        first = Topic.objects.create(course=self.course, title="Первая", order=2)
        self.assertEqual(list(Topic.objects.all()), [self.topic, second, first])
        a = Task.objects.create(topic=self.topic, title="a", order=2)
        b = Task.objects.create(topic=self.topic, title="b", order=0)
        c = Task.objects.create(topic=self.topic, title="c", order=0)
        self.assertEqual(list(self.topic.tasks.all()), [b, c, a])

    def test_negative_order_rejected(self):
        for obj in (Topic(course=self.course, title="t", order=-1),
                    Task(topic=self.topic, title="t", order=-1)):
            with self.assertRaises(ValidationError):
                obj.full_clean()

    def test_activation_validation(self):
        for field in ("question", "answer"):
            for value in ("", " \n\t "):
                with self.subTest(field=field, value=value):
                    data = dict(topic=self.topic, title="t", question="Текст", answer="001", is_active=True)
                    data[field] = value
                    with self.assertRaises(ValidationError) as error:
                        Task.objects.create(**data)
                    self.assertIn(field, error.exception.message_dict)
        self.assertEqual(Task.objects.count(), 0)

    def test_activation_and_answer_string(self):
        task = Task.objects.create(topic=self.topic, title="t", question="Текст", answer="001", is_active=True)
        task.refresh_from_db()
        self.assertEqual(task.answer, "001")
        self.assertTrue(task.is_active)

    def test_form_trims_only_edges(self):
        form = TaskForm(data={"topic": self.topic.pk, "title": "t", "question": " q ",
                              "answer": "  001 Ab  C  ", "is_active": True, "order": 0})
        self.assertTrue(form.is_valid(), form.errors)
        task = form.save()
        self.assertEqual(task.answer, "001 Ab  C")
        self.assertEqual(task.question, "q")

    def test_protected_parents(self):
        Task.objects.create(topic=self.topic, title="t")
        with self.assertRaises(ProtectedError):
            self.topic.delete()
        with self.assertRaises(ProtectedError):
            self.course.delete()


class AdminTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="attachment-test-")
        self.addCleanup(directory.cleanup)
        self.media = Path(directory.name)
        settings = override_settings(MEDIA_ROOT=directory.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.teacher = get_user_model().objects.create_superuser("teacher", password="test-password")
        self.client.force_login(self.teacher)
        self.course = Course.objects.create(title="Курс")
        self.topic = Topic.objects.create(course=self.course, title="Тема")
        self.task = Task.objects.create(topic=self.topic, title="Черновик")
        self.change_url = reverse("admin:materials_task_change", args=[self.task.pk])
        self.download_url = reverse("admin:materials_task_attachment", args=[self.task.pk, 99999])

    def data(self, **changes):
        return {"topic": self.topic.pk, "title": "Правка", "question": "Условие",
                "answer": " 001 ", "order": 0, "is_active": "on",
                "attachments-TOTAL_FORMS": 0, "attachments-INITIAL_FORMS": 0, **changes}

    def upload(self, content=b"first"):
        return SimpleUploadedFile("example.txt", content, content_type="text/plain")

    def test_admin_creation_and_editing(self):
        self.assertEqual(self.client.post(reverse("admin:materials_course_add"),
                                         {"title": "Другой курс", "description": ""}).status_code, 302)
        self.assertEqual(self.client.post(reverse("admin:materials_topic_add"),
                                         {"course": self.course.pk, "title": "Другая тема", "order": 1}).status_code, 302)
        self.assertEqual(self.client.post(reverse("admin:materials_task_add"), self.data()).status_code, 302)
        self.assertEqual(self.client.post(self.change_url, self.data()).status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Правка")
        self.assertEqual(self.task.answer, "001")
        self.assertTrue(self.task.is_active)
        self.assertContains(self.client.get(reverse("admin:materials_task_changelist")), "Правка")

    def test_invalid_activation_with_attachment(self):
        response = self.client.post(self.change_url, self.data(question="  ", **{"attachments-TOTAL_FORMS": 1, "attachments-0-file": self.upload()}))
        self.assertContains(response, "Для активации заполните текст условия.")
        self.task.refresh_from_db()
        self.assertFalse(self.task.is_active)
        self.assertFalse(self.task.attachments.exists())

    def test_deactivate_and_no_admin_delete(self):
        self.client.post(self.change_url, self.data())
        response = self.client.post(reverse("admin:materials_task_changelist"),
                                    {"action": "deactivate", "_selected_action": [self.task.pk]})
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertFalse(self.task.is_active)
        url = reverse("admin:materials_task_delete", args=[self.task.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 403)
        response = self.client.get(reverse("admin:materials_task_changelist"))
        self.assertNotContains(response, 'value="delete_selected"')
        self.client.post(reverse("admin:materials_task_changelist"),
                         {"action": "delete_selected", "_selected_action": [self.task.pk], "post": "yes"})
        self.assertTrue(Task.objects.filter(pk=self.task.pk).exists())

    def test_multiple_upload_replace_remove_and_download(self):
        data = self.data(**{"attachments-TOTAL_FORMS": 2,
                           "attachments-0-file": self.upload(b"first"),
                           "attachments-1-file": self.upload(b"second")})
        self.assertEqual(self.client.post(self.change_url, data).status_code, 302)
        a, b = self.task.attachments.all()
        old_path = Path(a.file.path)
        self.assertNotEqual(a.file.name, b.file.name)
        self.assertEqual(old_path.read_bytes(), b"first")
        self.assertEqual(Path(b.file.path).read_bytes(), b"second")
        for attachment, content in ((a, b"first"), (b, b"second")):
            url = reverse("admin:materials_task_attachment", args=[self.task.pk, attachment.pk])
            self.assertContains(self.client.get(self.change_url), url)
            self.assertNotContains(self.client.get(self.change_url), attachment.file.url)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment;", response["Content-Disposition"])
            self.assertEqual(b"".join(response.streaming_content), content)
            response.close()
        data = self.data(**{"attachments-TOTAL_FORMS": 2, "attachments-INITIAL_FORMS": 2,
                           "attachments-0-id": a.pk, "attachments-0-file": self.upload(b"replacement"),
                           "attachments-1-id": b.pk, "attachments-1-DELETE": "on"})
        self.assertEqual(self.client.post(self.change_url, data).status_code, 302)
        a.refresh_from_db()
        self.assertEqual(Path(a.file.path).read_bytes(), b"replacement")
        self.assertEqual(old_path.read_bytes(), b"first")
        self.assertTrue(Path(b.file.path).exists())
        self.assertEqual(self.task.attachments.count(), 1)
        self.assertEqual(self.client.get(reverse("admin:materials_task_attachment", args=[self.task.pk, b.pk])).status_code, 404)

    def test_attachment_cannot_be_accessed_through_another_task(self):
        other = Task.objects.create(topic=self.topic, title="Other")
        attachment = TaskAttachment.objects.create(task=other, file=self.upload())
        url = reverse("admin:materials_task_attachment", args=[self.task.pk, attachment.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_download_permissions(self):
        attachment = TaskAttachment.objects.create(task=self.task, file=self.upload())
        self.download_url = reverse("admin:materials_task_attachment", args=[self.task.pk, attachment.pk])
        self.client.logout()
        self.assertEqual(self.client.get(self.download_url).status_code, 302)
        user = get_user_model().objects.create_user("other", password="password")
        user.user_permissions.add(Permission.objects.get(codename="view_task"))
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.download_url).status_code, 302)
        user.is_staff = True
        user.save()
        response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, 200)
        response.close()
        user.user_permissions.clear()
        self.assertEqual(self.client.get(self.download_url).status_code, 403)
        user.user_permissions.add(Permission.objects.get(codename="change_task"))
        self.assertEqual(self.client.get(self.download_url).status_code, 403)

    def test_missing_file_and_unknown_task(self):
        self.assertEqual(self.client.get(self.download_url).status_code, 404)
        attachment = TaskAttachment.objects.create(task=self.task, file="tasks/missing.txt")
        self.download_url = reverse("admin:materials_task_attachment", args=[self.task.pk, attachment.pk])
        self.assertContains(self.client.get(self.download_url), "Файл вложения не найден.", status_code=404)
        self.assertEqual(self.client.get(reverse("admin:materials_task_attachment", args=[99999, 99999])).status_code, 404)
        self.assertEqual(self.client.get("/media/tasks/missing.txt").status_code, 404)


class AttachmentMigrationTests(TransactionTestCase):
    def test_existing_attachment_reference_is_preserved(self):
        executor = MigrationExecutor(connection)
        old_target = [("materials", "0001_initial")]
        new_target = [("materials", "0002_remove_task_attachment_taskattachment")]
        executor.migrate(old_target)
        try:
            apps = executor.loader.project_state(old_target).apps
            course = apps.get_model("materials", "Course").objects.create(title="Course")
            topic = apps.get_model("materials", "Topic").objects.create(course=course, title="Topic")
            Task = apps.get_model("materials", "Task")
            task = Task.objects.create(topic=topic, title="Existing", attachment="tasks/original.txt")
            Task.objects.create(topic=topic, title="Empty")
            executor = MigrationExecutor(connection)
            executor.migrate(new_target)
            apps = executor.loader.project_state(new_target).apps
            attachments = apps.get_model("materials", "TaskAttachment").objects.all()
            self.assertEqual(attachments.count(), 1)
            self.assertEqual(attachments.get().task_id, task.pk)
            self.assertEqual(attachments.get().file.name, "tasks/original.txt")
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
