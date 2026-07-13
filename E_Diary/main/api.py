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
    """Return cases that need eCourts fetching.

    Two groups:
      - pending:  ecourts_status='pending' (queued by user)
      - refresh:  any case with CNR where latest next_date ≤ today
                  (hearing passed; may have new business entries).
                  Non-clickable cases get purpose-of-hearing fallback.
                  Uses Asia/Kolkata date.
    """
    resp = _check_token(request)
    if resp:
        return resp

    today = timezone.localtime(timezone.now()).date()
    force = request.GET.get('force') == 'true'
    update_only = request.GET.get('update') == 'true'

    from django.db.models import Max, OuterRef, Subquery

    latest_next = DiaryEntry.objects.filter(
        case=OuterRef('pk'), entry_type='business'
    ).values('case').annotate(
        max_date=Max('next_date')
    ).values('max_date')

    cutoff = timezone.now() - datetime.timedelta(hours=6)

    if force:
        all_cases = Case.objects.filter(cnr__isnull=False).exclude(cnr='')
        pending = all_cases.filter(ecourts_status='pending')
        refresh = all_cases
    elif update_only:
        pending = Case.objects.none()
        refresh = Case.objects.filter(
            cnr__isnull=False, ecourts_last_checked__isnull=True
        ).exclude(cnr='').annotate(
            latest_next_date=Subquery(latest_next)
        ).filter(
            latest_next_date__isnull=False, latest_next_date__lte=today
        ) | Case.objects.filter(
            cnr__isnull=False, ecourts_last_checked__lt=cutoff
        ).exclude(cnr='').annotate(
            latest_next_date=Subquery(latest_next)
        ).filter(
            latest_next_date__isnull=False, latest_next_date__lte=today
        )
        refresh = refresh.distinct()[:15]
    else:
        pending = Case.objects.filter(
            ecourts_status='pending', cnr__isnull=False
        ).exclude(cnr='')

        refresh = Case.objects.filter(
            cnr__isnull=False, ecourts_last_checked__isnull=True
        ).exclude(cnr='').annotate(
            latest_next_date=Subquery(latest_next)
        ).filter(
            latest_next_date__isnull=False, latest_next_date__lte=today
        ) | Case.objects.filter(
            cnr__isnull=False, ecourts_last_checked__lt=cutoff
        ).exclude(cnr='').annotate(
            latest_next_date=Subquery(latest_next)
        ).filter(
            latest_next_date__isnull=False, latest_next_date__lte=today
        )
        refresh = refresh.distinct()[:15]

    def _serialize(case_qs):
        items = []
        for c in case_qs:
            already_fetched = list(
                DiaryEntry.objects.filter(
                    case=c, entry_type='business'
                ).exclude(ecourts_business='').exclude(ecourts_business__isnull=True)
                .values_list('previous_date', flat=True)
            )
            items.append({
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
        return items

    return JsonResponse({
        'pending': _serialize(pending),
        'pending_total': pending.count(),
        'refresh': _serialize(refresh),
        'refresh_total': refresh.count(),
    })


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

        existing = DiaryEntry.objects.filter(
            case=case, previous_date=biz_date, entry_type='business'
        ).first()

        if existing:
            existing.ecourts_business = biz_text
            existing.business_summary = existing.business or biz_text
            if next_hearing:
                existing.next_date = next_hearing
            if stage:
                existing.stage = stage
            existing._skip_summary_update = True
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
                business_summary=biz_text,
                next_date=next_hearing or biz_date,
            )
            created += 1

    # Update case status
    if not ecourts_available and not created and not updated:
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
