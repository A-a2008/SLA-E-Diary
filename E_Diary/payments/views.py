import json
import logging
from decimal import Decimal
from io import BytesIO
from datetime import datetime

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User, Group
from django.contrib import messages
from django.db import transaction
from django.db.models import OuterRef, Subquery, F, Q
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.http import HttpResponse, Http404, JsonResponse
from django.template.loader import render_to_string
from django.views.decorators.http import require_http_methods

from main.models import Case, DiaryEntry
from .decorators import payments_access_required, superuser_required
from .models import (
    ChargeType, CasePricing, CaseChargeAmount, CustomCharge,
    OneTimeExtra, EntryClassification, EntryChargeItem, DiaryEntryPayment
)
from .services import (
    get_or_create_pricing, get_applicable_charge_types,
    is_cc_criminal, classify_and_setup, generate_pdf, generate_png_from_pdf
)

logger = logging.getLogger(__name__)



# ── DASHBOARD ──

@payments_access_required
def dashboard(request):
    unpaid_entries = DiaryEntryPayment.objects.filter(is_paid=False).select_related('diary_entry__case')
    total_unpaid = sum(
        float(sum(ci.amount for ci in EntryChargeItem.objects.filter(
            entry_classification__diary_entry=up.diary_entry
        )))
        for up in unpaid_entries
    )
    total_cases = Case.objects.count()
    fully_paid = CasePricing.objects.filter(fully_paid=True).count()
    return render(request, 'payments/dashboard.html', {
        'unpaid_count': unpaid_entries.count(),
        'total_unpaid': f'\u20b9{int(total_unpaid):,}',
        'total_cases': total_cases,
        'fully_paid': fully_paid,
    })


# ── CASE LIST (paginated, ordered by most recent entry) ──

@payments_access_required
def case_list(request):
    q = request.GET.get('q', '').strip()

    latest_entry = DiaryEntry.objects.filter(
        case=OuterRef('pk'), entry_type='business'
    ).order_by('-previous_date').values('previous_date')[:1]

    cases = Case.objects.annotate(
        latest_entry_date=Subquery(latest_entry)
    ).select_related('pricing').order_by(
        F('latest_entry_date').desc(nulls_last=True), '-id'
    )

    if q:
        cases = cases.filter(
            Q(case_type__icontains=q) |
            Q(case_number__icontains=q) |
            Q(party_1__icontains=q) |
            Q(party_2__icontains=q)
        )

    paginator = Paginator(cases, 10)
    page = request.GET.get('page', 1)
    try:
        page_obj = paginator.get_page(page)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.get_page(1)

    case_data = []
    for case in page_obj:
        pricing = get_or_create_pricing(case)
        entries = case.diary_entries.filter(entry_type='business')
        total_unpaid = 0
        unpaid_count = 0
        for entry in entries:
            try:
                pay = entry.payment_info
                if not pay.is_paid:
                    items = EntryChargeItem.objects.filter(
                        entry_classification__diary_entry=entry
                    )
                    total_items = sum(float(i.amount or 0) for i in items)
                    total_unpaid += total_items
                    unpaid_count += 1
            except DiaryEntryPayment.DoesNotExist:
                unpaid_count += 1
        case_data.append({
            'case': case,
            'pricing': pricing,
            'unpaid_count': unpaid_count,
            'total_unpaid': f'\u20b9{int(total_unpaid):,}' if total_unpaid else '--',
        })
    return render(request, 'payments/case_list.html', {
        'page_obj': page_obj, 'cases': case_data, 'q': q,
    })


# ── CASE PRICING ──

@payments_access_required
def case_pricing(request, case_id):
    case = get_object_or_404(Case, id=case_id)
    pricing = get_or_create_pricing(case)
    charge_types = get_applicable_charge_types(case)
    if request.method == 'POST':
        with transaction.atomic():
            pricing.client_name = request.POST.get('client_name', '')
            pricing.client_phone = request.POST.get('client_phone', '')
            pricing.is_one_time = request.POST.get('is_one_time') == 'on'
            pricing.one_time_amount = request.POST.get('one_time_amount') or None
            pricing.appearance_included = request.POST.get('appearance_included') == 'on'
            pricing.appearance_amount = request.POST.get('appearance_amount') or None
            pricing.save()
            for ct in charge_types:
                key = f'amount_{ct.id}'
                val = request.POST.get(key, '').strip()
                cca, _ = CaseChargeAmount.objects.get_or_create(
                    case_pricing=pricing, charge_type=ct
                )
                cca.amount = val if val else None
                cca.save()
            custom_ids = request.POST.getlist('custom_id[]')
            custom_names = request.POST.getlist('custom_name[]')
            custom_amounts = request.POST.getlist('custom_amount[]')
            existing_custom_ids = set()
            for i in range(len(custom_names)):
                cid = custom_ids[i] if i < len(custom_ids) else ''
                name = custom_names[i].strip()
                amt = custom_amounts[i].strip() if i < len(custom_amounts) else ''
                if not name:
                    continue
                if cid and cid.isdigit():
                    cc = CustomCharge.objects.get(id=int(cid), case_pricing=pricing)
                    cc.name = name
                    cc.amount = amt if amt else None
                    cc.save()
                    existing_custom_ids.add(int(cid))
                else:
                    cc = CustomCharge.objects.create(
                        case_pricing=pricing, name=name,
                        amount=amt if amt else None
                    )
                    existing_custom_ids.add(cc.id)
            CustomCharge.objects.filter(case_pricing=pricing).exclude(
                id__in=existing_custom_ids
            ).delete() if existing_custom_ids else CustomCharge.objects.filter(
                case_pricing=pricing
            ).delete()
            ote_ids = request.POST.getlist('ote_id[]')
            ote_names = request.POST.getlist('ote_name[]')
            ote_included = request.POST.getlist('ote_included[]')
            ote_amounts = request.POST.getlist('ote_amount[]')
            existing_ote_ids = set()
            for i in range(len(ote_names)):
                oid = ote_ids[i] if i < len(ote_ids) else ''
                name = ote_names[i].strip()
                included = (ote_included[i] if i < len(ote_included) else '') == 'on'
                amt = ote_amounts[i].strip() if i < len(ote_amounts) else ''
                if not name:
                    continue
                if oid and oid.isdigit():
                    ote = OneTimeExtra.objects.get(id=int(oid), case_pricing=pricing)
                    ote.name = name
                    ote.included_in_one_time = included
                    ote.per_occurrence_amount = amt if amt else None
                    ote.save()
                    existing_ote_ids.add(int(oid))
                else:
                    ote = OneTimeExtra.objects.create(
                        case_pricing=pricing, name=name,
                        included_in_one_time=included,
                        per_occurrence_amount=amt if amt else None
                    )
                    existing_ote_ids.add(ote.id)
            OneTimeExtra.objects.filter(case_pricing=pricing).exclude(
                id__in=existing_ote_ids
            ).delete() if existing_ote_ids else OneTimeExtra.objects.filter(
                case_pricing=pricing
            ).delete()
        messages.success(request, 'Pricing saved successfully.')
        return redirect('payments:case_pricing', case_id=case.id)
    charge_amounts = {
        cca.charge_type_id: cca
        for cca in CaseChargeAmount.objects.filter(case_pricing=pricing)
    }
    custom_charges = CustomCharge.objects.filter(case_pricing=pricing)
    one_time_extras = OneTimeExtra.objects.filter(case_pricing=pricing)
    entries = case.diary_entries.filter(entry_type='business').order_by('-previous_date')
    total_entries = entries.count()
    paid_count = 0
    unpaid_count = 0
    total_billed = Decimal('0')
    total_collected = Decimal('0')
    for entry in entries:
        try:
            pay = entry.payment_info
            items = EntryChargeItem.objects.filter(
                entry_classification__diary_entry=entry
            )
            entry_total = sum((i.amount or Decimal('0')) for i in items)
            total_billed += entry_total
            if pay.is_paid:
                paid_count += 1
                total_collected += entry_total
            else:
                unpaid_count += 1
        except DiaryEntryPayment.DoesNotExist:
            unpaid_count += 1
    total_due = total_billed - total_collected
    return render(request, 'payments/case_pricing.html', {
        'case': case,
        'pricing': pricing,
        'charge_types': charge_types,
        'charge_amounts': charge_amounts,
        'custom_charges': custom_charges,
        'one_time_extras': one_time_extras,
        'paid_count': paid_count,
        'unpaid_count': unpaid_count,
        'total_billed': total_billed,
        'total_collected': total_collected,
        'total_due': total_due,
        'total_entries': total_entries,
        'is_cc_criminal': is_cc_criminal(case),
    })


# ── CASE ENTRIES ──

@payments_access_required
def case_entries(request, case_id):
    case = get_object_or_404(Case, id=case_id)
    pricing = get_or_create_pricing(case)
    entries = case.diary_entries.filter(entry_type='business').order_by('-previous_date')
    entry_data = []
    for entry in entries:
        try:
            pay = entry.payment_info
        except DiaryEntryPayment.DoesNotExist:
            pay = DiaryEntryPayment.objects.create(diary_entry=entry)
        try:
            classification = entry.classification
            items = classification.charge_items.all()
        except EntryClassification.DoesNotExist:
            items = []
        total = sum(float(i.amount or 0) for i in items)
        entry_data.append({
            'entry': entry,
            'payment': pay,
            'charge_items': items,
            'total': f'\u20b9{int(total):,}' if total == int(total) else f'\u20b9{total:,.2f}',
            'charge_labels': ', '.join(
                (i.charge_type.name if i.charge_type else i.custom_charge_name)
                for i in items
            ) if items else 'Not classified',
        })
    return render(request, 'payments/case_entries.html', {
        'case': case,
        'pricing': pricing,
        'entry_data': entry_data,
    })


# ── PAYMENTS DUE ──

@payments_access_required
def payments_due(request):
    q = request.GET.get('q', '').strip()
    unpaid_payments = DiaryEntryPayment.objects.filter(
        is_paid=False
    ).select_related('diary_entry__case').order_by('diary_entry__case', '-diary_entry__previous_date')
    if q:
        from django.db.models import Q
        unpaid_payments = unpaid_payments.filter(
            Q(diary_entry__case__case_type__icontains=q) |
            Q(diary_entry__case__case_number__icontains=q) |
            Q(diary_entry__case__party_1__icontains=q) |
            Q(diary_entry__case__party_2__icontains=q)
        )
    cases_dict = {}
    for pay in unpaid_payments:
        case = pay.diary_entry.case
        if case.id not in cases_dict:
            pricing = get_or_create_pricing(case)
            cases_dict[case.id] = {
                'case': case,
                'pricing': pricing,
                'entries': [],
                'total_due': 0,
            }
        items = EntryChargeItem.objects.filter(
            entry_classification__diary_entry=pay.diary_entry
        )
        entry_total = sum(float(i.amount or 0) for i in items)
        cases_dict[case.id]['entries'].append({
            'entry': pay.diary_entry,
            'payment': pay,
            'charge_items': items,
            'total': entry_total,
            'charge_labels': ', '.join(
                (i.charge_type.name if i.charge_type else i.custom_charge_name)
                for i in items
            ),
        })
        cases_dict[case.id]['total_due'] += entry_total
    return render(request, 'payments/payments_due.html', {
        'cases_dict': cases_dict.values(), 'q': q,
    })


# ── EDIT CLASSIFICATION ──

@payments_access_required
def edit_classification(request, entry_id):
    entry = get_object_or_404(DiaryEntry, id=entry_id)
    case = entry.case
    pricing = get_or_create_pricing(case)
    classification, created = EntryClassification.objects.get_or_create(
        diary_entry=entry,
        defaults={'auto_classified': True}
    )
    charge_types = get_applicable_charge_types(case)
    custom_charges = CustomCharge.objects.filter(case_pricing=pricing)
    if request.method == 'POST':
        with transaction.atomic():
            selected_ids = request.POST.getlist('charge_types')
            classification.auto_classified = False
            classification.classified_by = request.user
            classification.save()
            classification.charge_items.all().delete()
            for ct_id in selected_ids:
                ct = ChargeType.objects.get(id=int(ct_id))
                cca = CaseChargeAmount.objects.filter(
                    case_pricing=pricing, charge_type=ct
                ).first()
                amount = cca.amount if cca and cca.amount else Decimal('0')
                EntryChargeItem.objects.create(
                    entry_classification=classification,
                    charge_type=ct,
                    amount=amount
                )
            custom_names = request.POST.getlist('custom_name[]')
            custom_amounts = request.POST.getlist('custom_amount[]')
            for i in range(len(custom_names)):
                name = custom_names[i].strip()
                amt = custom_amounts[i].strip() if i < len(custom_amounts) else '0'
                if name:
                    EntryChargeItem.objects.create(
                        entry_classification=classification,
                        charge_type=None,
                        custom_charge_name=name,
                        amount=Decimal(amt) if amt else Decimal('0')
                    )
        DiaryEntryPayment.objects.get_or_create(
            diary_entry=entry, defaults={'is_paid': False}
        )
        messages.success(request, 'Classification updated.')
        return redirect('payments:case_entries', case_id=case.id)
    selected = set(
        classification.charge_items.filter(
            charge_type__isnull=False
        ).values_list('charge_type_id', flat=True)
    )
    custom_items = classification.charge_items.filter(charge_type__isnull=True)
    return render(request, 'payments/entry_classify.html', {
        'entry': entry,
        'case': case,
        'charge_types': charge_types,
        'selected': selected,
        'custom_items': custom_items,
        'custom_charges': custom_charges,
        'charge_amounts': {
            cca.charge_type_id: cca
            for cca in CaseChargeAmount.objects.filter(case_pricing=pricing)
        },
    })


# ── TOGGLE PAYMENT ──

@require_http_methods(['POST'])
@payments_access_required
def toggle_payment(request, entry_id):
    entry = get_object_or_404(DiaryEntry, id=entry_id)
    pay, _ = DiaryEntryPayment.objects.get_or_create(diary_entry=entry)
    pay.is_paid = not pay.is_paid
    if pay.is_paid:
        pay.paid_at = datetime.now()
        pay.paid_by = request.user
    else:
        pay.paid_at = None
        pay.paid_by = None
    pay.save()
    messages.success(request, f'Entry marked as {"PAID" if pay.is_paid else "UNPAID"}.')
    return redirect(request.META.get('HTTP_REFERER', '/payments/due/'))


# ── BATCH PAY ──

@require_http_methods(['POST'])
@payments_access_required
def batch_pay(request):
    entry_ids = request.POST.getlist('entry_ids')
    for eid in entry_ids:
        entry = DiaryEntry.objects.filter(id=eid).first()
        if entry:
            pay, _ = DiaryEntryPayment.objects.get_or_create(diary_entry=entry)
            pay.is_paid = True
            pay.paid_at = datetime.now()
            pay.paid_by = request.user
            pay.save()
    messages.success(request, f'{len(entry_ids)} entries marked as paid.')
    return redirect(request.META.get('HTTP_REFERER', '/payments/due/'))


# ── MARK CASE FULL PAID ──

@require_http_methods(['POST'])
@payments_access_required
def mark_full_paid(request, case_id):
    case = get_object_or_404(Case, id=case_id)
    pricing = get_or_create_pricing(case)
    pricing.fully_paid = not pricing.fully_paid
    pricing.fully_paid_at = datetime.now() if pricing.fully_paid else None
    pricing.save()
    if pricing.fully_paid:
        for entry in case.diary_entries.filter(entry_type='business'):
            pay, _ = DiaryEntryPayment.objects.get_or_create(diary_entry=entry)
            pay.is_paid = True
            pay.paid_at = datetime.now()
            pay.paid_by = request.user
            pay.save()
    messages.success(request, f'Case marked as {"FULLY PAID" if pricing.fully_paid else "NOT FULLY PAID"}.')
    return redirect(request.META.get('HTTP_REFERER', '/payments/cases/'))


# ── RECLASSIFY ──

@require_http_methods(['POST'])
@payments_access_required
def reclassify_single(request, entry_id):
    entry = get_object_or_404(DiaryEntry, id=entry_id)
    try:
        classify_and_setup(entry)
        messages.success(request, 'Re-classified successfully.')
    except TimeoutError:
        logger.error(f"Reclassification timed out for entry {entry_id}")
        messages.error(request, 'Classification timed out. Try again later.')
    except Exception as e:
        logger.error(f"Reclassification failed: {e}", exc_info=True)
        messages.error(request, f'Classification failed: {e}')
    return redirect(request.META.get('HTTP_REFERER', '/payments/cases/'))


@require_http_methods(['POST'])
@payments_access_required
def reclassify_case(request, case_id):
    case = get_object_or_404(Case, id=case_id)
    entries = case.diary_entries.filter(entry_type='business')
    success = 0
    for entry in entries:
        try:
            classify_and_setup(entry)
            success += 1
        except Exception as e:
            logger.error(f"Reclassification failed for entry {entry.id}: {e}")
    messages.success(request, f'Re-classified {success}/{entries.count()} entries.')
    return redirect(request.META.get('HTTP_REFERER', '/payments/cases/'))


# ── QUICK CLASSIFY (AJAX from diary entry case page) ──

@require_http_methods(['POST'])
@payments_access_required
def quick_classify(request, entry_id):
    entry = get_object_or_404(DiaryEntry, id=entry_id)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)
    charge_type_ids = data.get('charge_types', [])
    pricing = get_or_create_pricing(entry.case)
    classification, _ = EntryClassification.objects.get_or_create(diary_entry=entry)
    classification.auto_classified = False
    classification.classified_by = request.user
    classification.save()
    classification.charge_items.all().delete()
    for ct_id in charge_type_ids:
        ct = get_object_or_404(ChargeType, id=int(ct_id))
        cca = CaseChargeAmount.objects.filter(case_pricing=pricing, charge_type=ct).first()
        amount = cca.amount if cca and cca.amount else Decimal('0')
        EntryChargeItem.objects.create(
            entry_classification=classification, charge_type=ct, amount=amount
        )
    return JsonResponse({'ok': True})


# ── INVOICE ──

def _invoice_context(entry, request=None):
    case = entry.case
    pricing = get_or_create_pricing(case)
    try:
        classification = entry.classification
        items = classification.charge_items.all()
    except EntryClassification.DoesNotExist:
        items = []
    try:
        payment = entry.payment_info
    except DiaryEntryPayment.DoesNotExist:
        payment = None
    total = sum(float(i.amount or 0) for i in items)
    custom_message = None
    if request:
        custom_message = request.GET.get('message', None) or None
    return {
        'case': case,
        'entry': entry,
        'pricing': pricing,
        'items': items,
        'payment': payment,
        'total': total,
        'custom_message': custom_message,
    }


@payments_access_required
def invoice_pdf(request, entry_id):
    entry = get_object_or_404(DiaryEntry, id=entry_id)
    ctx = _invoice_context(entry, request)
    html = render_to_string('payments/invoice_pdf.html', ctx)
    pdf = generate_pdf(html)
    filename = f'invoice_{ctx["case"].case_type}_{ctx["case"].case_number}_{ctx["case"].case_year}_{entry.id}.pdf'.replace('/', '_')
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@payments_access_required
def invoice_image(request, entry_id):
    entry = get_object_or_404(DiaryEntry, id=entry_id)
    ctx = _invoice_context(entry, request)
    html = render_to_string('payments/invoice_pdf.html', ctx)
    pdf_bytes = generate_pdf(html).read()
    try:
        png_bytes = generate_png_from_pdf(pdf_bytes)
    except Exception as e:
        logger.error(f"PNG generation failed: {e}")
        return HttpResponse("Image generation failed. PDF is available instead.", status=500)
    filename = f'invoice_{ctx["case"].case_type}_{ctx["case"].case_number}_{ctx["case"].case_year}_{entry.id}.png'.replace('/', '_')
    return HttpResponse(png_bytes, content_type='image/png',
                        headers={'Content-Disposition': f'attachment; filename="{filename}"'})


# ── FEE AGREEMENT ──

@payments_access_required
def fee_agreement_pdf(request, case_id):
    case = get_object_or_404(Case, id=case_id)
    pricing = get_or_create_pricing(case)
    charge_types = get_applicable_charge_types(case)
    charge_amounts_list = []
    for ct in charge_types:
        cca = CaseChargeAmount.objects.filter(case_pricing=pricing, charge_type=ct).first()
        charge_amounts_list.append({
            'name': ct.name,
            'amount': cca.amount if cca else None,
        })
    for cc in CustomCharge.objects.filter(case_pricing=pricing):
        charge_amounts_list.append({
            'name': cc.name,
            'amount': cc.amount,
        })
    one_time_extras = OneTimeExtra.objects.filter(case_pricing=pricing)
    html = render_to_string('payments/fee_agreement_pdf.html', {
        'case': case,
        'pricing': pricing,
        'charge_amounts': charge_amounts_list,
        'one_time_extras': one_time_extras,
        'today': datetime.now().strftime('%d %B %Y'),
    })
    pdf = generate_pdf(html)
    filename = f'fee_agreement_{case.case_type}_{case.case_number}_{case.case_year}.pdf'.replace('/', '_')
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@payments_access_required
def fee_agreement_image(request, case_id):
    case = get_object_or_404(Case, id=case_id)
    pricing = get_or_create_pricing(case)
    charge_types = get_applicable_charge_types(case)
    charge_amounts_list = []
    for ct in charge_types:
        cca = CaseChargeAmount.objects.filter(case_pricing=pricing, charge_type=ct).first()
        charge_amounts_list.append({
            'name': ct.name,
            'amount': cca.amount if cca else None,
        })
    for cc in CustomCharge.objects.filter(case_pricing=pricing):
        charge_amounts_list.append({
            'name': cc.name,
            'amount': cc.amount,
        })
    one_time_extras = OneTimeExtra.objects.filter(case_pricing=pricing)
    html = render_to_string('payments/fee_agreement_pdf.html', {
        'case': case,
        'pricing': pricing,
        'charge_amounts': charge_amounts_list,
        'one_time_extras': one_time_extras,
        'today': datetime.now().strftime('%d %B %Y'),
    })
    pdf_bytes = generate_pdf(html).read()
    try:
        png_bytes = generate_png_from_pdf(pdf_bytes)
    except Exception as e:
        logger.error(f"PNG generation failed: {e}")
        return HttpResponse("Image generation failed. PDF is available instead.", status=500)
    filename = f'fee_agreement_{case.case_type}_{case.case_number}_{case.case_year}.png'.replace('/', '_')
    return HttpResponse(png_bytes, content_type='image/png',
                        headers={'Content-Disposition': f'attachment; filename="{filename}"'})


# ── USER MANAGEMENT (superuser only) ──

@superuser_required
def manage_payments_users(request):
    group = Group.objects.get_or_create(name='payments')[0]
    members = group.user_set.all().select_related('userprofile').order_by('username')
    non_members = User.objects.exclude(groups=group).exclude(is_superuser=True).order_by('username')
    return render(request, 'payments/manage_users.html', {
        'members': members,
        'non_members': non_members,
    })


@require_http_methods(['POST'])
@superuser_required
def add_payments_user(request):
    user_id = request.POST.get('user_id')
    user = get_object_or_404(User, id=user_id)
    group = Group.objects.get_or_create(name='payments')[0]
    user.groups.add(group)
    messages.success(request, f'{user.get_full_name() or user.username} added to payments group.')
    return redirect('payments:manage_payments_users')


@require_http_methods(['POST'])
@superuser_required
def remove_payments_user(request, user_id):
    user = get_object_or_404(User, id=user_id)
    group = Group.objects.get(name='payments')
    user.groups.remove(group)
    messages.success(request, f'{user.get_full_name() or user.username} removed from payments group.')
    return redirect('payments:manage_payments_users')
