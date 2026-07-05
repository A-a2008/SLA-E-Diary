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

from django.conf import settings

from .models import Case, DiaryEntry
from .constants import COURT_LABELS

logger = logging.getLogger(__name__)

# Add project root so we can import ecourt_scraper.session
# ecourts_integration.py -> main/ -> E_Diary/ -> project root (3 levels up)
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from ecourt_scraper.session import (
    EcourtSession,
    set_call_limit,
    reset_call_counter,
    can_call,
)

# ---- config ----
ECOURTS_CALL_LIMIT = 200  # enough for the full history of any case
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_FALLBACK_MODEL = "openai/gpt-oss-20b"
GROQ_FINAL_FALLBACK = "qwen/qwen3-32b"


def _get_groq():
    from langchain_groq import ChatGroq

    api_key = getattr(settings, "GROQ_API_KEY", None) or os.getenv("GROQ_API_KEY")
    if not api_key:
        from dotenv import load_dotenv
        load_dotenv()
        api_key = os.getenv("GROQ_API_KEY")

    if not api_key:
        return None

    for model in [GROQ_MODEL, GROQ_FALLBACK_MODEL, GROQ_FINAL_FALLBACK]:
        try:
            llm = ChatGroq(api_key=api_key, model=model, temperature=0, max_retries=1, timeout=30)
            from langchain_core.prompts import ChatPromptTemplate
            chain = ChatPromptTemplate.from_messages([
                ("system", "Respond concisely."),
                ("human", "ok"),
            ]) | llm
            chain.invoke({})
            return llm
        except Exception:
            continue
    return None


def summarize_business(advocate_text: str, ecourts_text: str, case=None) -> str:
    """Use Groq to merge advocate notes + eCourts data into one accurate summary.

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

    llm = _get_groq()
    if not llm:
        return f"{advocate_text}\n\n(From eCourts: {ecourts_text})"

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

    from langchain_core.prompts import ChatPromptTemplate

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a legal assistant for an Indian law firm. Your job is to merge two descriptions of the SAME court hearing into a single accurate, readable summary.

RULES — STRICTLY FOLLOW:
1. PRESERVE ALL FACTS — do not add, remove, reword, or "improve" any factual content. Do not guess what abbreviations stand for. Keep acronyms as-is (e.g. "DHR" stays "DHR", "IA" stays "IA").
2. If both descriptions say the same thing, output either one — they agree.
3. If the advocate's notes are more detailed, use them as the base and weave in any extra detail from the eCourts record.
4. If the eCourts record has extra detail the advocate omitted, incorporate it naturally.
5. Output 1-3 sentences. Be concise but complete.
6. NEVER invent explanations for abbreviations — just keep them as they appear.

PAST CASE HISTORY is provided for context only — it shows how this case has progressed, so you understand what abbreviations like DHR, IA, KMC etc. mean in THIS case. Do not include past history in the output unless it's directly relevant to understanding today's entry."""),
        ("human", "PAST CASE HISTORY (for context only):\n{history}\n\nAdvocate's Notes:\n{advocate}\n\neCourts Record:\n{ecourts}"),
    ])

    try:
        chain = prompt | llm
        result = chain.invoke({
            "history": history_block,
            "advocate": advocate_text,
            "ecourts": ecourts_text,
        })
        return (result.content if hasattr(result, "content") else str(result)).strip()
    except Exception as e:
        logger.warning(f"Groq summarization failed: {e}")
        return f"{advocate_text}\n\n(From eCourts: {ecourts_text})"


# ---- date helpers ----

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
        details = session.search_case(cnr)

        if not session.ecourts_available:
            result["ecourts_available"] = False
            return result

        links = session.get_business_links()
        result["total_available"] = len(links)
        skipped = 0

        for i, link in enumerate(links):
            link_date = (link.get("business_date") or "").strip()

            if link_date in skip_dates:
                skipped += 1
                continue

            try:
                biz = session.view_business(link)
            except RuntimeError as e:
                if "limit" in str(e).lower():
                    result["limit_reached"] = True
                else:
                    result["error"] = str(e)
                break

            biz_date = _parse_date_dmy(link.get("business_date"))
            if not biz_date:
                biz_date = _parse_date_dmy(biz.get("date"))

            ecourts_biz = biz.get("business", "").strip()
            if not ecourts_biz:
                continue

            next_hearing = _parse_date_dmy(biz.get("next_hearing_date"))
            stage = biz.get("next_purpose", "").strip()

            result["items"].append({
                "business_date": biz_date,
                "business": ecourts_biz,
                "next_hearing": next_hearing,
                "stage": stage,
            })

            if i < len(links) - 1:
                session.back_to_history()

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

    if not scraped_data["ecourts_available"]:
        result["ecourts_available"] = False
        result["success"] = True
        result["errors"].append("Case does not support eCourts business lookup (family matter or no clickable links)")
        from django.utils import timezone
        case.ecourts_status = 'unsupported'
        case.ecourts_last_checked = timezone.now()
        case.save()
        return result

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
            existing.business_summary = summarize_business(existing.business, biz_text, case=case)
            existing.save()
            result["entries_updated"] += 1
        else:
            court_label = COURT_LABELS.get(case.court, case.court)
            entry = DiaryEntry.objects.create(
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
            entry.business_summary = summarize_business("", biz_text, case=case)
            entry.save()
            result["entries_created"] += 1

    if scraped_data.get("limit_reached"):
        result["limit_reached"] = True

    # Set ecourts_status on the case
    if not scraped_data.get("ecourts_available", True):
        case.ecourts_status = 'unsupported'
    elif not result["entries_created"] and not result["entries_updated"]:
        case.ecourts_status = 'no_data'
    else:
        case.ecourts_status = 'done'
    from django.utils import timezone
    case.ecourts_last_checked = timezone.now()
    case.save()

    result["success"] = True

    return result


def batch_update_all_cases() -> dict:
    """Update all cases with CNR from eCourts. Respects the 5-call limit."""
    reset_call_counter()
    set_call_limit(ECOURTS_CALL_LIMIT)

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
