"""One-time teacher provisioning must not reset existing credentials."""
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from deploy.bootstrap import create_teacher, take_credentials


class TeacherBootstrapTests(TestCase):
    def test_password_is_removed_before_children_can_inherit_it(self):
        with patch.dict(os.environ, {
            'DJANGO_SUPERUSER_USERNAME': 'teacher',
            'DJANGO_SUPERUSER_EMAIL': 'teacher@example.org',
            'DJANGO_SUPERUSER_PASSWORD': 'one-time-test-value',
        }):
            self.assertEqual(take_credentials(),
                             ('teacher', 'teacher@example.org', 'one-time-test-value'))
            self.assertNotIn('DJANGO_SUPERUSER_PASSWORD', os.environ)

    def test_first_teacher_is_created_once_and_never_reset(self):
        password = 'isolated-test-Q7!bR9%xL2'
        self.assertTrue(create_teacher(('teacher', '', password)))
        teacher = get_user_model().objects.get(username='teacher')
        self.assertTrue(teacher.is_active and teacher.is_staff and teacher.is_superuser)
        self.assertTrue(teacher.check_password(password))
        self.assertFalse(create_teacher(('teacher', '', 'replacement-test-password')))
        teacher.refresh_from_db()
        self.assertTrue(teacher.check_password(password))
        self.assertFalse(create_teacher(('another-teacher', '', password)))
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_invalid_password_does_not_create_teacher(self):
        with self.assertRaises(ValidationError):
            create_teacher(('teacher', '', '123'))
        self.assertFalse(get_user_model().objects.exists())

    def test_no_secret_leaves_database_unmodified(self):
        self.assertFalse(create_teacher(('teacher', '', None)))
        self.assertFalse(get_user_model().objects.exists())
