import logging
import json
import os
import time
import threading
from decimal import Decimal
from io import BytesIO
from datetime import datetime

from django.db import transaction as db_transaction
from django.db import models as db_models
from django.utils import timezone

from main.models import Case, DiaryEntry
from .models import (
    ChargeType, CasePricing, CaseChargeAmount, CustomCharge,
    EntryClassification, EntryChargeItem, DiaryEntryPayment,
    Client, CaseClient, Invoice, Transaction, TransactionCase
)

logger = logging.getLogger(__name__)

# ── Client Balance & Ledger ──

def get_client_balance(client):
    invoices = Invoice.objects.filter(client=client).aggregate(
        total=db_models.Sum('amount'))['total'] or Decimal('0')
    payments = Transaction.objects.filter(client=client).aggregate(
        total=db_models.Sum('amount'))['total'] or Decimal('0')
    return payments - invoices


def get_client_ledger(client):
    rows = []
    invoices = Invoice.objects.filter(client=client).order_by('created_at')
    for inv in invoices:
        rows.append({
            'date': inv.created_at,
            'type': 'Invoice',
            'ref': inv.invoice_no,
            'case': str(inv.case),
            'particulars': inv.particulars,
            'debit': inv.amount,
            'credit': Decimal('0'),
        })
    payments = Transaction.objects.filter(client=client).order_by('transaction_date')
    for txn in payments:
        case_str = ', '.join(str(tc.case) for tc in txn.cases.all()) if txn.cases.exists() else '—'
        rows.append({
            'date': txn.transaction_date,
            'type': 'Payment',
            'ref': f"TXN-{txn.id}",
            'case': case_str,
            'particulars': f"{txn.get_payment_method_display()}{' (' + txn.other_method_detail + ')' if txn.other_method_detail else ''} {txn.notes}",
            'debit': Decimal('0'),
            'credit': txn.amount,
        })
    rows.sort(key=lambda r: r['date'])
    balance = Decimal('0')
    for r in rows:
        balance += r['credit'] - r['debit']
        r['balance'] = balance
    return rows


def get_case_ledger(case):
    rows = []
    invoices = Invoice.objects.filter(case=case).order_by('created_at')
    for inv in invoices:
        client_name = inv.client.name if inv.client else '—'
        rows.append({
            'date': inv.created_at,
            'type': 'Invoice',
            'ref': inv.invoice_no,
            'client': client_name,
            'particulars': inv.particulars,
            'debit': inv.amount,
            'credit': Decimal('0'),
        })
    txn_ids = TransactionCase.objects.filter(case=case).values_list('transaction_id', flat=True)
    payments = Transaction.objects.filter(id__in=list(txn_ids)).order_by('transaction_date')
    for txn in payments:
        rows.append({
            'date': txn.transaction_date,
            'type': 'Payment',
            'ref': f"TXN-{txn.id}",
            'client': txn.client.name,
            'particulars': f"{txn.get_payment_method_display()}{' (' + txn.other_method_detail + ')' if txn.other_method_detail else ''} {txn.notes}",
            'debit': Decimal('0'),
            'credit': txn.amount,
        })
    rows.sort(key=lambda r: r['date'])
    balance = Decimal('0')
    for r in rows:
        balance += r['credit'] - r['debit']
        r['balance'] = balance
    return rows


def get_all_outstanding():
    clients = Client.objects.annotate(
        total_invoiced=db_models.Sum('invoice_set__amount'),
        total_paid=db_models.Sum('transactions__amount'),
    )
    result = []
    for c in clients:
        invoiced = c.total_invoiced or Decimal('0')
        paid = c.total_paid or Decimal('0')
        balance = paid - invoiced
        if balance < 0:
            result.append({'client': c, 'balance': balance, 'invoiced': invoiced, 'paid': paid})
    result.sort(key=lambda r: r['balance'])
    return result


# ── Invoice Number Generation ──

@db_transaction.atomic
def generate_invoice_no(case):
    year = datetime.now().year
    prefix = f"{case.id}-{year}-"
    last = Invoice.objects.filter(invoice_no__startswith=prefix
    ).order_by('invoice_no').values_list('invoice_no', flat=True).last()
    if last:
        num = int(last.split('-')[-1]) + 1
    else:
        num = 1
    return f"{prefix}{num:04d}"


def sync_invoice(classification):
    entry = classification.diary_entry
    case = entry.case
    items = classification.charge_items.all()
    if not items:
        return None
    total = sum((i.amount or Decimal('0')) for i in items)
    particulars = ', '.join(
        i.charge_type.name if i.charge_type else i.custom_charge_name
        for i in items
    )
    first_client = CaseClient.objects.filter(case=case).first()
    client = first_client.client if first_client else None
    invoice, created = Invoice.objects.get_or_create(
        diary_entry=entry,
        defaults={
            'invoice_no': generate_invoice_no(case),
            'client': client,
            'case': case,
            'particulars': particulars,
            'amount': total,
        }
    )
    if not created:
        invoice.client = client or invoice.client
        invoice.particulars = particulars
        invoice.amount = total
        invoice.save()
    return invoice


# ── Existing helpers ──

CONSUMER_COURT_CODES = {'consumer', 'urban_consumer', 'state_consumer_basava'}


def is_cc_criminal(case):
    if 'CC' not in case.case_type.upper():
        return False
    return case.court not in CONSUMER_COURT_CODES


def get_or_create_pricing(case):
    pricing, created = CasePricing.objects.get_or_create(case=case)
    return pricing


def get_applicable_charge_types(case):
    types = ChargeType.objects.all()
    representing = case.representing
    party_1 = case.party_1_type
    party_2 = case.party_2_type
    if representing == party_1:
        types = types.filter(
            db_models.Q(applies_to=ChargeType.BOTH) |
            db_models.Q(applies_to=ChargeType.PETITIONER)
        )
    elif representing == party_2:
        types = types.filter(
            db_models.Q(applies_to=ChargeType.BOTH) |
            db_models.Q(applies_to=ChargeType.RESPONDENT)
        )
    else:
        types = types.filter(applies_to=ChargeType.BOTH)
    if not is_cc_criminal(case):
        types = types.exclude(requires_cc_criminal=True)
    return types.order_by('position', 'name')


def compute_entry_amounts(entry, charge_codes, custom_names=None):
    case = entry.case
    pricing = get_or_create_pricing(case)
    amounts = []
    for code in charge_codes:
        ct = ChargeType.objects.filter(code=code).first()
        if not ct:
            continue
        cca = CaseChargeAmount.objects.filter(case_pricing=pricing, charge_type=ct).first()
        amount = cca.amount if cca and cca.amount else Decimal('0')
        amounts.append({'charge_type': ct, 'amount': amount})
    if custom_names:
        for cname in custom_names:
            amounts.append({'charge_type': None, 'custom_charge_name': cname, 'amount': Decimal('0')})
    return amounts


def classify_and_setup(entry):
    from .classifier import classify_business_entry
    case = entry.case
    pricing = get_or_create_pricing(case)
    charge_codes = classify_business_entry(entry)
    classification, created = EntryClassification.objects.get_or_create(
        diary_entry=entry,
        defaults={'auto_classified': True}
    )
    if not created:
        classification.charge_items.all().delete()
        classification.auto_classified = True
        classification.save()
    for code in charge_codes:
        ct = ChargeType.objects.filter(code=code).first()
        if not ct:
            continue
        cca = CaseChargeAmount.objects.filter(case_pricing=pricing, charge_type=ct).first()
        amount = cca.amount if cca and cca.amount else Decimal('0')
        EntryChargeItem.objects.create(
            entry_classification=classification,
            charge_type=ct,
            amount=amount
        )
    DiaryEntryPayment.objects.get_or_create(
        diary_entry=entry,
        defaults={'is_paid': False}
    )
    sync_invoice(classification)
    return classification


NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_CLASSIFIER_MODELS = [
    "openai/gpt-oss-20b",
]
_nvidia_lock = threading.Lock()
_nvidia_last_call = 0
_NVIDIA_MIN_INTERVAL = 2.0


def _wait_nvidia():
    global _nvidia_last_call
    with _nvidia_lock:
        now = time.monotonic()
        elapsed = now - _nvidia_last_call
        if elapsed < _NVIDIA_MIN_INTERVAL:
            time.sleep(_NVIDIA_MIN_INTERVAL - elapsed)
        _nvidia_last_call = time.monotonic()


def _nvidia_chat(model, messages_list, temperature=0, max_tokens=4096, timeout=60, rate_limit=True):
    from openai import OpenAI
    api_key = os.getenv('NVIDIA_API_KEY')
    if not api_key:
        logger.warning("NVIDIA_API_KEY not set")
        return None
    client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key, timeout=timeout)
    if rate_limit:
        _wait_nvidia()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages_list,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        logger.warning(f"NVIDIA API error (model={model}, timeout={timeout}s): {e}")
        return None


def generate_pdf(html_string):
    from weasyprint import HTML
    pdf_file = BytesIO()
    HTML(string=html_string).write_pdf(pdf_file)
    pdf_file.seek(0)
    return pdf_file


def generate_png_from_pdf(pdf_bytes):
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    page = doc[0]
    pix = page.get_pixmap(dpi=200)
    img_bytes = pix.tobytes('png')
    doc.close()
    return img_bytes
