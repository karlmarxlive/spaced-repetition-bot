"""Durable transport state; no references or raw Telegram updates in payloads."""
from django.db import models


class IncomingEvent(models.Model):
    bot_id = models.PositiveBigIntegerField()
    update_id = models.BigIntegerField()
    received_at = models.DateTimeField()
    processed_at = models.DateTimeField(null=True)
    state = models.CharField(max_length=12, default='processing')
    student = models.ForeignKey('users.Student', null=True, on_delete=models.PROTECT)
    attempt = models.ForeignKey('study_sessions.Attempt', null=True, on_delete=models.PROTECT)
    session = models.ForeignKey('study_sessions.StudySession', null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['bot_id', 'update_id'], name='unique_bot_update')]


class QuestionDelivery(models.Model):
    attempt = models.OneToOneField('study_sessions.Attempt', on_delete=models.PROTECT)
    # Absent row means unknown stage-6 delivery. New questions always get a row.
    state = models.CharField(max_length=12, default='pending')
    delivered_at = models.DateTimeField(null=True)
    last_message_id = models.BigIntegerField(null=True)
    operation = models.CharField(max_length=120)


class OutgoingMessage(models.Model):
    bot_id = models.PositiveBigIntegerField()
    chat_id = models.BigIntegerField()
    student = models.ForeignKey('users.Student', null=True, on_delete=models.PROTECT)
    attempt = models.ForeignKey('study_sessions.Attempt', null=True, on_delete=models.PROTECT)
    session = models.ForeignKey('study_sessions.StudySession', null=True, on_delete=models.PROTECT)
    operation = models.CharField(max_length=120)
    part = models.PositiveIntegerField()
    kind = models.CharField(max_length=12, default='text')
    is_question = models.BooleanField(default=False)
    text = models.TextField(blank=True)
    path = models.TextField(blank=True)
    filename = models.CharField(max_length=255, blank=True)
    state = models.CharField(max_length=12, default='pending', choices=[
        ('pending', 'Ожидает'), ('sending', 'Отправляется'), ('retry', 'Повтор'),
        ('sent', 'Отправлено'), ('failed', 'Окончательная ошибка'), ('cancelled', 'Отменено')])
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    sent_at = models.DateTimeField(null=True)
    message_id = models.BigIntegerField(null=True)
    error = models.CharField(max_length=32, blank=True)
    lease_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)

    class Meta:
        ordering = ['pk']
        constraints = [models.UniqueConstraint(fields=['bot_id', 'operation', 'part'], name='unique_outgoing_part')]
        indexes = [models.Index(fields=['bot_id', 'chat_id', 'state']), models.Index(fields=['state', 'next_attempt_at'])]
