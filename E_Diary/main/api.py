import os
import json
import datetime
import logging

from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

from .models import OutgoingMessage, Case, DiaryEntry
from .constants import COURT_LABELS

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


@csrf_exempt
@require_GET
def ecourts_pending(request):
    """Return cases that need eCourts fetching — pending + done cases for recheck."""
    resp = _check_token(request)
    if resp:
        return resp

    now = timezone.now()
    cutoff = now - datetime.timedelta(hours=24)

    pending = Case.objects.filter(
        ecourts_status='pending', cnr__isnull=False
    ).exclude(cnr='')

    recheck = Case.objects.filter(
        ecourts_status='done', ecourts_last_checked__lt=cutoff, cnr__isnull=False
    ).exclude(cnr='')

    cases = list(pending[:20]) + list(recheck[:10])

    data = []
    for c in cases:
        already_fetched = list(
            DiaryEntry.objects.filter(
                case=c, entry_type='business'
            ).exclude(ecourts_business='').exclude(ecourts_business__isnull=True)
            .values_list('previous_date', flat=True)
        )
        data.append({
            'id': c.id,
            'cnr': c.cnr,
            'case_type': c.case_type,
            'case_number': c.case_number,
            'case_year': c.case_year,
            'court': c.court,
            'court_hall': c.court_hall,
            'floor': c.floor,
            'representing': c.representing,
            'representing_parties': c.representing_parties,
            'party_1_total': c.party_1_total,
            'party_2_total': c.party_2_total,
            'already_fetched_dates': [d.strftime('%d-%m-%Y') for d in already_fetched if d],
        })

    return JsonResponse({'cases': data, 'pending_total': pending.count()})


@csrf_exempt
@require_POST
def ecourts_upsert(request):
    """Accept scraped eCourts data from the laptop and upsert DiaryEntry records."""
    resp = _check_token(request)
    if resp:
        return resp

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    case_id = body.get('case_id')
    status = body.get('status', '')
    entries = body.get('entries', [])
    ecourts_available = body.get('ecourts_available', True)

    if not case_id:
        return JsonResponse({'error': 'case_id required'}, status=400)

    try:
        case = Case.objects.get(id=case_id)
    except Case.DoesNotExist:
        return JsonResponse({'error': 'Case not found'}, status=404)

    from .ecourts_integration import summarize_business, cleanup_ecourts_text

    def _parse_d(v):
        if not v:
            return None
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
            try:
                return datetime.datetime.strptime(str(v), fmt).date()
            except ValueError:
                continue
        return None

    created = 0
    updated = 0

    for item in entries:
        biz_date = _parse_d(item.get('previous_date'))
        if not biz_date:
            continue
        biz_text = (item.get('business') or '').strip()
        if not biz_text:
            continue
        next_hearing = _parse_d(item.get('next_hearing'))
        stage = (item.get('stage') or '').strip()

        biz_text, stage = cleanup_ecourts_text(biz_text, stage)

        existing = DiaryEntry.objects.filter(
            case=case, previous_date=biz_date, entry_type='business'
        ).first()

        if existing:
            existing.ecourts_business = biz_text
            existing.business_summary = summarize_business(
                existing.business, biz_text, case=case
            )
            if next_hearing:
                existing.next_date = next_hearing
            if stage:
                existing.stage = stage
            existing.save()
            updated += 1
        else:
            court_label = COURT_LABELS.get(case.court, case.court)
            DiaryEntry.objects.create(
                case=case,
                entry_type='business',
                previous_date=biz_date,
                court=court_label,
                court_hall=case.court_hall,
                floor=case.floor,
                case_number_display=f"{case.case_type}/{case.case_number}/{case.case_year}",
                representing=case.representing,
                representing_parties=case.representing_parties,
                party_1_total=case.party_1_total,
                party_2_total=case.party_2_total,
                stage=stage,
                business='',
                ecourts_business=biz_text,
                business_summary=summarize_business('', biz_text, case=case),
                next_date=next_hearing or biz_date,
            )
            created += 1

    # Update case status
    if not ecourts_available:
        case.ecourts_status = 'unsupported'
    elif not entries and status != 'done':
        case.ecourts_status = 'no_data'
    else:
        case.ecourts_status = status or 'done'
    case.ecourts_last_checked = timezone.now()
    case.save()

    return JsonResponse({
        'ok': True,
        'case_id': case_id,
        'created': created,
        'updated': updated,
        'status': case.ecourts_status,
    })
