"""Integration layer between the Django site and the eCourts scraper.

Handles:
  - Fetching all eCourts business history for a case
  - Upserting DiaryEntry records with ecourts_business
  - Combining advocate notes + eCourts data in the business_summary
  - Detecting cases that don't support eCourts (family matters, etc.)
"""

import datetime
import os
import sys
import logging

from .models import Case, DiaryEntry
from .constants import COURT_LABELS

logger = logging.getLogger(__name__)

# Add project root so we can import ecourt_scraper.session
# ecourts_integration.py -> main/ -> E_Diary/ -> project root (3 levels up)
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ecourt_scraper.session imported lazily inside functions that need it
# (Playwright not available on PythonAnywhere)

# ---- config ----
ECOURTS_CALL_LIMIT = 200  # enough for the full history of any case

import json
import threading
import time

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODELS = [
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.5-397b-a17b",
]

# ---- NVIDIA rate limiter (30 RPM = 2s interval) ----
_nvidia_lock = threading.Lock()
_nvidia_last_call: float = 0
_NVIDIA_MIN_INTERVAL = 2.0


def _wait_nvidia():
    global _nvidia_last_call
    with _nvidia_lock:
        now = time.monotonic()
        elapsed = now - _nvidia_last_call
        if elapsed < _NVIDIA_MIN_INTERVAL:
            time.sleep(_NVIDIA_MIN_INTERVAL - elapsed)
        _nvidia_last_call = time.monotonic()


def _nvidia_chat(model: str, messages: list, temperature: float = 0, max_tokens: int = 4096) -> str | None:
    """Send a chat completion to NVIDIA with rate-limit enforcement."""
    from openai import OpenAI

    api_key = os.getenv("NVIDIA_API_KEY")
    if not api_key:
        from dotenv import load_dotenv
        load_dotenv()
        api_key = os.getenv("NVIDIA_API_KEY")
    if not api_key:
        logger.error("NVIDIA_API_KEY not set")
        return None

    client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key)
    try:
        _wait_nvidia()
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        logger.warning(f"NVIDIA API error (model={model}): {e}")
        return None


def summarize_business(advocate_text: str, ecourts_text: str, case=None) -> str:
    """Use NVIDIA LLM to merge advocate notes + eCourts data into one accurate summary.

    Pass the Case object so past diary entries are included as context,
    helping the AI understand abbreviations and the case history.
    """
    advocate_text = (advocate_text or "").strip()
    ecourts_text = (ecourts_text or "").strip()

    if not advocate_text and not ecourts_text:
        return ""
    if not ecourts_text:
        return advocate_text
    if not advocate_text:
        return ecourts_text

    # Build case-history context
    history_lines = []
    if case:
        past = case.diary_entries.filter(entry_type='business').exclude(
            business=None
        ).exclude(business='').order_by('-previous_date')[:10]
        for e in past:
            src = e.business or e.ecourts_business or ""
            if src.strip():
                d = e.previous_date.strftime('%d-%m-%Y') if e.previous_date else '?'
                history_lines.append(f"[{d}] {src.strip()}")
    history_block = "\n".join(history_lines) if history_lines else "No prior entries available."

    system = (
        "You are a legal assistant for an Indian law firm. Your job is to merge two descriptions of the SAME court hearing into a single accurate, readable summary.\n\n"
        "RULES — STRICTLY FOLLOW:\n"
        "1. PRESERVE ALL FACTS — do not add, remove, reword, or 'improve' any factual content. Do not guess what abbreviations stand for. Keep acronyms as-is (e.g. 'DHR' stays 'DHR', 'IA' stays 'IA').\n"
        "2. If both descriptions say the same thing, output either one — they agree.\n"
        "3. If the advocate's notes are more detailed, use them as the base and weave in any extra detail from the eCourts record.\n"
        "4. If the eCourts record has extra detail the advocate omitted, incorporate it naturally.\n"
        "5. Output 1-3 sentences. Be concise but complete.\n"
        "6. NEVER invent explanations for abbreviations — just keep them as they appear.\n\n"
        "PAST CASE HISTORY is provided for context only — it shows how this case has progressed, so you understand what abbreviations like DHR, IA, KMC etc. mean in THIS case. Do not include past history in the output unless it's directly relevant to understanding today's entry."
    )
    user_msg = (
        f"PAST CASE HISTORY (for context only):\n{history_block}\n\n"
        f"Advocate's Notes:\n{advocate_text}\n\n"
        f"eCourts Record:\n{ecourts_text}"
    )

    for model in NVIDIA_MODELS:
        result = _nvidia_chat(model, [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ], temperature=0.6)
        if result:
            logger.info(f"NVIDIA summarise used model: {model}")
            return result.strip()

    logger.warning("NVIDIA summarise: all models failed, using fallback")
    return f"{advocate_text}\n\n(From eCourts: {ecourts_text})"


# ---- date helpers ----

def cleanup_ecourts_text(business_text: str, stage_text: str = "") -> tuple:
    """Use NVIDIA LLM to fix capitalization, punctuation, and obvious typos in eCourts text.

    Returns (cleaned_business, cleaned_stage) — if LLM fails, returns originals.
    """
    business_text = (business_text or "").strip()
    stage_text = (stage_text or "").strip()

    if not business_text and not stage_text:
        return (business_text, stage_text)

    system = (
        "You are a legal text formatter. Fix capitalization, punctuation, and obvious typos in court records.\n"
        "RULES:\n"
        "1. Do NOT change any factual content, case numbers, dates, names, legal terms, section numbers, or abbreviations.\n"
        "2. Keep all acronyms exactly as they appear (DHR, IA, KMC, CrPC, IPC, etc.).\n"
        "3. Only fix: capitalization (first letter of sentences, proper nouns), punctuation (missing periods, commas), extra spaces, and clear typos.\n"
        "4. If the text is in ALL CAPS, convert to normal sentence case while preserving proper nouns and acronyms.\n"
        "5. Return a JSON object with keys \"business\" and \"stage\"."
    )

    for model in NVIDIA_MODELS:
        user_msg = json.dumps({"business": business_text, "stage": stage_text})
        result = _nvidia_chat(model, [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ])
        if result is None:
            continue
        try:
            parsed = json.loads(result)
            cleaned_biz = (parsed.get("business") or business_text).strip()
            cleaned_stage = (parsed.get("stage") or stage_text).strip()
            logger.info(f"NVIDIA cleanup used model: {model}")
            return (cleaned_biz, cleaned_stage)
        except (json.JSONDecodeError, TypeError):
            continue

    logger.warning("NVIDIA cleanup: all models failed, keeping original")
    return (business_text, stage_text)


def _parse_date_dmy(date_str: str):
    """Parse DD-MM-YYYY string to date object."""
    if not date_str:
        return None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _scrape_case(cnr: str, skip_dates: set = None) -> dict:
    """Run the headless scraper (no Django ORM).
    skip_dates: set of date strings 'DD-MM-YYYY' to skip (already fetched).
    Returns dict with items, ecourts_available, error, limit_reached, total_available.
    """
    result = {"items": [], "ecourts_available": True, "error": None, "limit_reached": False, "total_available": 0}
    skip_dates = skip_dates or set()

    session = EcourtSession()
    try:
        session.search_case(cnr)

        purpose_items = session.get_purpose_hearings()
        for item in purpose_items:
            biz_date = _parse_date_dmy(item.get("business_date"))
            if not biz_date:
                continue
            date_str = biz_date.strftime('%d-%m-%Y')
            if date_str in skip_dates:
                continue
            purpose = (item.get("purpose") or "").strip()
            if not purpose:
                continue
            result["items"].append({
                "business_date": biz_date,
                "business": purpose.title(),
                "next_hearing": _parse_date_dmy(item.get("hearing_date")),
                "stage": purpose.title(),
            })
        result["total_available"] = len(purpose_items)
        result["ecourts_available"] = bool(purpose_items)

    except RuntimeError as e:
        if "limit" in str(e).lower():
            result["limit_reached"] = True
        result["error"] = str(e)
    except Exception as e:
        result["error"] = str(e)
    finally:
        session.close()

    return result


# ---- public API ----

def can_fetch_ecourts(case: Case) -> bool:
    return bool(case.cnr and case.cnr.strip())


def fetch_and_update_case(case: Case) -> dict:
    """
    Main entry point. Scrape eCourts for a case's CNR and upsert DiaryEntry records.

    Returns:
        dict with keys: success, entries_updated, entries_created, errors, limit_reached, ecourts_available
    """
    from ecourt_scraper.session import EcourtSession, set_call_limit, reset_call_counter

    reset_call_counter()
    set_call_limit(ECOURTS_CALL_LIMIT)

    result = {
        "success": False,
        "entries_updated": 0,
        "entries_created": 0,
        "errors": [],
        "limit_reached": False,
        "ecourts_available": True,
    }

    if not case.cnr:
        result["errors"].append("No CNR for this case")
        return result

    # Build set of already-fetched dates (DD-MM-YYYY format) so we skip them
    existing_dates = set()
    for entry in DiaryEntry.objects.filter(case=case, entry_type='business').exclude(ecourts_business='').exclude(ecourts_business__isnull=True):
        if entry.previous_date:
            existing_dates.add(entry.previous_date.strftime('%d-%m-%Y'))

    # Step 1: scrape data (no Django ORM inside Playwright's async context)
    scraped_data = _scrape_case(case.cnr, skip_dates=existing_dates)
    if scraped_data["error"]:
        result["errors"].append(scraped_data["error"])
        if scraped_data.get("limit_reached"):
            result["limit_reached"] = True
        return result

    result["ecourts_available"] = scraped_data.get("ecourts_available", True)
    result["total_available"] = scraped_data.get("total_available", 0)

    # Step 2: process scraped data with Django ORM (outside async context)
    for item in scraped_data["items"]:
        biz_date = item["business_date"]
        biz_text = item["business"]
        next_hearing = item["next_hearing"]
        stage = item["stage"]

        existing = DiaryEntry.objects.filter(
            case=case,
            previous_date=biz_date,
            entry_type='business',
        ).first()

        if existing:
            existing.ecourts_business = biz_text
            existing.business_summary = existing.business or biz_text
            existing.stage = stage or existing.stage
            if next_hearing:
                existing.next_date = next_hearing
            existing._skip_summary_update = True
            existing.save()
            result["entries_updated"] += 1
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
            result["entries_created"] += 1

    if scraped_data.get("limit_reached"):
        result["limit_reached"] = True

    # Set ecourts_status on the case
    if not result["entries_created"] and not result["entries_updated"]:
        case.ecourts_status = 'no_data' if result["ecourts_available"] else 'unsupported'
    else:
        case.ecourts_status = 'done'
    from django.utils import timezone
    case.ecourts_last_checked = timezone.now()
    case.save()

    result["success"] = True

    return result


def batch_update_all_cases() -> dict:
    """Update all cases with CNR from eCourts. Respects the 5-call limit."""
    cases = Case.objects.exclude(cnr='').exclude(cnr__isnull=True)

    summary = {
        "total": cases.count(),
        "updated": 0,
        "created": 0,
        "errors": [],
        "limit_reached": False,
    }

    for case in cases:
        reset_call_counter()
        r = fetch_and_update_case(case)
        summary["updated"] += r.get("entries_updated", 0)
        summary["created"] += r.get("entries_created", 0)
        if r.get("errors"):
            summary["errors"].extend(r["errors"])
        if r.get("limit_reached"):
            summary["limit_reached"] = True
            break

    return summary
