import os
import logging

from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET
from dotenv import load_dotenv

from .models import OutgoingMessage

load_dotenv()

logger = logging.getLogger(__name__)

API_TOKEN = os.getenv('API_TOKEN')


def _check_token(request):
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        auth = auth[7:]
    if not auth.strip():
        auth = request.GET.get('token', '')
    if not API_TOKEN or auth != API_TOKEN:
        return JsonResponse({'error': 'Unauthorized'}, status=401)


@csrf_exempt
@require_GET
def pending_messages(request):
    resp = _check_token(request)
    if resp:
        return resp
    msgs = OutgoingMessage.objects.filter(sent=False).order_by('created_at')
    data = [{'id': m.id, 'chat_id': m.chat_id, 'text': m.text} for m in msgs]
    return JsonResponse({'messages': data})


@csrf_exempt
@require_POST
def mark_sent(request, msg_id):
    resp = _check_token(request)
    if resp:
        return resp
    try:
        msg = OutgoingMessage.objects.get(id=msg_id, sent=False)
        msg.sent = True
        msg.sent_at = timezone.now()
        msg.save()
        return JsonResponse({'ok': True})
    except OutgoingMessage.DoesNotExist:
        return JsonResponse({'error': 'Not found or already sent'}, status=404)
