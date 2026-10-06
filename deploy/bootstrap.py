"""Create the first teacher from a short-lived deployment secret."""
import os


def take_credentials():
    # Remove the password before migrations, Gunicorn or Telegram are spawned.
    password = os.environ.pop('DJANGO_SUPERUSER_PASSWORD', None)
    username = os.environ.pop('DJANGO_SUPERUSER_USERNAME', 'admin')
    email = os.environ.pop('DJANGO_SUPERUSER_EMAIL', '')
    return username, email, password


def create_teacher(credentials):
    from django.contrib.auth import get_user_model
    from django.contrib.auth.password_validation import validate_password
    from django.db import transaction

    username, email, password = credentials
    if password is None:
        return False
    user_model = get_user_model()
    with transaction.atomic():
        # Never reset a teacher's password during a restart or deployment.
        if user_model.objects.filter(is_superuser=True).exists():
            return False
        user = user_model(username=username, email=email, is_staff=True,
                          is_superuser=True, is_active=True)
        validate_password(password, user)
        user.set_password(password)
        user.save(force_insert=True)
    return True
