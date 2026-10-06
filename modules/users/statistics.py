"""Read-only, student-scoped Django Admin views."""
from datetime import datetime, time, timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import FileResponse, Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import reverse

from config.repetitions import ERROR_RETRY_DAYS, SUCCESS_INTERVAL_DAYS
from modules.delivery.models import OutgoingMessage, QuestionDelivery
from modules.repetitions.scheduling import MOSCOW
from modules.study_sessions.models import Attempt
from modules.users.models import Student


ERROR_LABELS = {
    'network': 'Сеть, timeout или временная ошибка Telegram',
    'rate_limit': 'Ограничение частоты Telegram',
    'chat_unavailable': 'Бот заблокирован или чат недоступен',
    'attachment': 'Вложение отсутствует или путь недопустим',
    'telegram_request': 'Telegram отклонил сообщение или вложение',
    'configuration': 'Telegram отклонил токен',
    'transport': 'Ошибка транспорта',
}


def authorize(model_admin, request, student_id):
    student = get_object_or_404(Student, pk=student_id)
    if not (model_admin.has_view_permission(request, student) and
            request.user.has_perm('study_sessions.view_attempt') and
            request.user.has_perm('repetitions.view_topicprogress') and
            request.user.has_perm('delivery.view_outgoingmessage')):
        raise PermissionDenied
    return student


def statistics(model_admin, request, student_id):
    student = authorize(model_admin, request, student_id)
    if request.method != 'GET':
        return HttpResponseNotAllowed(['GET'])
    assignments = list(student.topic_assignments.select_related('topic', 'progress').order_by('topic__order', 'pk'))
    for assignment in assignments:
        step = assignment.progress.interval_step
        assignment.interval_label = (f'Ступень 0: первое повторение / после ошибки — {ERROR_RETRY_DAYS} дн.'
                                     if step == 0 else f'{SUCCESS_INTERVAL_DAYS[step - 1]} дн.')
    attempts = Attempt.objects.filter(student=student).select_related('assignment__topic')
    topic = request.GET.get('topic', '')
    if topic:
        attempts = attempts.filter(assignment__topic_id=topic) if topic.isdecimal() else attempts.none()
    status = request.GET.get('status', '')
    if status:
        attempts = attempts.filter(status=status) if status in ('open', 'cancelled', 'correct', 'incorrect') else attempts.none()
    for key, lookup in [('from', 'opened_at__gte'), ('to', 'opened_at__lt')]:
        value = request.GET.get(key, '')
        if value:
            try:
                day = datetime.strptime(value, '%Y-%m-%d').date()
                if key == 'to':
                    day += timedelta(days=1)
                attempts = attempts.filter(**{lookup: datetime.combine(day, time.min, tzinfo=MOSCOW)})
            except ValueError:
                attempts = attempts.none()
    counts = {item['status']: item['total'] for item in attempts.values('status').annotate(total=Count('pk'))}
    history = Paginator(attempts.order_by('-opened_at', '-pk'), 25).get_page(request.GET.get('page'))
    pending = OutgoingMessage.objects.filter(student=student).exclude(state__in=['sent', 'cancelled']).order_by('pk')
    outgoing = Paginator(pending, 25).get_page(request.GET.get('delivery_page'))
    for message in outgoing:
        message.error_label = ERROR_LABELS.get(message.error, '—')
    query = request.GET.copy()
    query.pop('page', None)
    context = dict(model_admin.admin_site.each_context(request), title=f'Учебная статистика: {student}',
        student=student, assignments=assignments, last_interval=SUCCESS_INTERVAL_DAYS[-1], history=history, outgoing=outgoing, counts=counts,
        filters=request.GET, page_query=query.urlencode(), current=Attempt.objects.filter(student=student, status='open').first(),
        delivery=QuestionDelivery.objects.filter(attempt__student=student, attempt__status='open').first())
    return TemplateResponse(request, 'admin/users/student_statistics.html', context)


def attempt_detail(model_admin, request, student_id, attempt_id):
    student = authorize(model_admin, request, student_id)
    if request.method != 'GET':
        return HttpResponseNotAllowed(['GET'])
    attempt = get_object_or_404(Attempt, pk=attempt_id, student=student)
    attachments = [{'name': item['name'], 'url': reverse('admin:users_student_attempt_file',
                   args=[student.pk, attempt.pk, index])} for index, item in enumerate(attempt.attachments)]
    return TemplateResponse(request, 'admin/users/student_attempt.html', dict(
        model_admin.admin_site.each_context(request), title=f'Попытка {attempt.pk}: {student}',
        student=student, attempt=attempt, attachments=attachments))


def attempt_file(model_admin, request, student_id, attempt_id, index):
    student = authorize(model_admin, request, student_id)
    if request.method != 'GET':
        return HttpResponseNotAllowed(['GET'])
    attempt = get_object_or_404(Attempt, student=student, pk=attempt_id)
    if index >= len(attempt.attachments):
        raise Http404
    item = attempt.attachments[index]
    root = Path(settings.MEDIA_ROOT).resolve()
    path = (root / item['path']).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise Http404
    return FileResponse(path.open('rb'), as_attachment=True, filename=item['name'])
