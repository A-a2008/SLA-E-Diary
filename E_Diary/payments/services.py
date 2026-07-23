import logging
import json
import os
import time
import threading
from decimal import Decimal
from io import BytesIO

from django.template.loader import render_to_string
from django.contrib.auth.models import User
from django.db import models as db_models

from .models import (
    ChargeType, CasePricing, CaseChargeAmount, CustomCharge,
    EntryClassification, EntryChargeItem, DiaryEntryPayment
)

logger = logging.getLogger(__name__)

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
    return classification


NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_CLASSIFIER_MODELS = [
    "openai/gpt-oss-120b",
    "mistralai/mistral-large-3-675b-instruct-2512",
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
