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

# ecourt_scraper.session imported lazily inside functions that need it
# (Playwright not available on PythonAnywhere)

# ---- config ----
ECOURTS_CALL_LIMIT = 200  # enough for the full history of any case
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_FALLBACK_MODEL = "openai/gpt-oss-20b"
GROQ_FINAL_FALLBACK = "qwen/qwen3-32b"

import threading
import httpx

# ---- Groq rate limiter (auto-tuned from API headers) ----
_groq_lock = threading.Lock()
_groq_last_call: float = 0
_groq_min_interval = 60.0 / 15  # 4.0s default


def _probe_groq_rate_limit(api_key: str) -> dict:
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(
                'https://api.groq.com/openai/v1/models',
                headers={'Authorization': f'Bearer {api_key}'},
            )
            headers = {}
            for key in resp.headers:
                lk = key.lower()
                if lk.startswith('x-ratelimit-'):
                    headers[lk] = resp.headers[key]
            return headers
    except Exception:
        return {}


def _auto_tune_groq_interval(headers: dict):
    global _groq_min_interval
    tpm_str = headers.get('x-ratelimit-limit-tokens')
    if tpm_str:
        try:
            tpm = int(tpm_str)
            tokens_per_req = 400
            rpm = max(1, min(tpm / tokens_per_req, 60))
            _groq_min_interval = max(60.0 / rpm, 1.0)
            logger.info(f'Groq TPM: {tpm} → ~{rpm:.0f} req/min at {tokens_per_req}t/req, '
                       f'interval set to {_groq_min_interval:.1f}s')
        except (ValueError, TypeError):
            pass


def _wait_groq():
    global _groq_last_call
    with _groq_lock:
        now = time.monotonic()
        elapsed = now - _groq_last_call
        if elapsed < _groq_min_interval:
            time.sleep(_groq_min_interval - elapsed)
        _groq_last_call = time.monotonic()


# ============================================================
# Groq API key rotation (time-based + on 429), .env update
# ============================================================

GROQ_API_KEYS = [k.strip() for k in os.getenv("GROQ_API_KEYS", "").split(",") if k.strip()]
if not GROQ_API_KEYS:
    single = os.getenv("GROQ_API_KEY", "").strip()
    if single:
        GROQ_API_KEYS = [single]
_groq_key_index = -1  # -1 = start with whatever is in .env
_last_groq_rotation = time.monotonic()
_groq_consecutive_429 = 0


def _update_env_groq_key(new_key: str):
    env_path = os.path.join(_project_root, 'E_Diary', '.env')
    if not os.path.exists(env_path):
        return
    lines = []
    found = False
    with open(env_path) as f:
        for line in f:
            if line.startswith('GROQ_API_KEY='):
                lines.append(f'GROQ_API_KEY={new_key}\n')
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f'GROQ_API_KEY={new_key}\n')
    with open(env_path, 'w') as f:
        f.writelines(lines)


def _rotate_groq_key():
    global _groq_key_index, _last_groq_rotation, _groq_consecutive_429
    _groq_key_index = (_groq_key_index + 1) % len(GROQ_API_KEYS)
    new_key = GROQ_API_KEYS[_groq_key_index]
    os.environ['GROQ_API_KEY'] = new_key
    _update_env_groq_key(new_key)
    _last_groq_rotation = time.monotonic()
    _groq_consecutive_429 += 1
    logger.warning(f"Rotated Groq key to #{_groq_key_index + 1}/{len(GROQ_API_KEYS)} "
                   f"(consecutive 429s: {_groq_consecutive_429})")
    if _groq_consecutive_429 >= len(GROQ_API_KEYS) * 3:
        logger.error("All %d Groq keys appear exhausted — daily quota likely reached",
                     len(GROQ_API_KEYS))


def _maybe_time_rotate_groq() -> bool:
    if time.monotonic() - _last_groq_rotation >= 60:
        _rotate_groq_key()
        return True
    return False


def _handle_groq_429(e: Exception):
    """Rotate key on 429, double interval."""
    global _groq_min_interval
    estr = str(e)
    if "429" in estr or "Too Many Requests" in estr or "rate_limit_exceeded" in estr:
        _groq_min_interval = min(_groq_min_interval * 2, 60.0)
        _rotate_groq_key()
        time.sleep(5)


def _get_groq():
    from langchain_groq import ChatGroq

    api_key = getattr(settings, "GROQ_API_KEY", None) or os.getenv("GROQ_API_KEY")
    if not api_key:
        from dotenv import load_dotenv
        load_dotenv()
        api_key = os.getenv("GROQ_API_KEY")

    if not api_key:
        return None

    # Probe Groq rate limits once at startup
    rate_headers = _probe_groq_rate_limit(api_key)
    if rate_headers:
        _auto_tune_groq_interval(rate_headers)

    for model in [GROQ_MODEL, GROQ_FALLBACK_MODEL, GROQ_FINAL_FALLBACK]:
        try:
            llm = ChatGroq(api_key=api_key, model=model, temperature=0, max_retries=1, timeout=30)
            from langchain_core.prompts import ChatPromptTemplate
            chain = ChatPromptTemplate.from_messages([
                ("system", "Respond concisely."),
                ("human", "ok"),
            ]) | llm
            _wait_groq()
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

    retries = len(GROQ_API_KEYS) + 1
    for attempt in range(retries):
        try:
            _wait_groq()
            _maybe_time_rotate_groq()
            chain = prompt | llm
            result = chain.invoke({
                "history": history_block,
                "advocate": advocate_text,
                "ecourts": ecourts_text,
            })
            return (result.content if hasattr(result, "content") else str(result)).strip()
        except Exception as e:
            estr = str(e)
            if "429" in estr or "Too Many Requests" in estr or "rate_limit_exceeded" in estr:
                logger.warning(f"Groq summarization 429 (attempt {attempt + 1}/{retries}): {e}")
                if attempt >= retries - 1:
                    break
                _handle_groq_429(e)
                llm = _get_groq()
                if not llm:
                    break
            else:
                logger.warning(f"Groq summarization failed: {e}")
                break
    return f"{advocate_text}\n\n(From eCourts: {ecourts_text})"


# ---- date helpers ----

def cleanup_ecourts_text(business_text: str, stage_text: str = "") -> tuple:
    """Use Groq to fix capitalization, punctuation, and obvious typos in eCourts text.

    Returns (cleaned_business, cleaned_stage) — if Groq fails, returns originals.
    Only fixes formatting; preserves all facts, names, abbreviations, and legal terms.
    """
    business_text = (business_text or "").strip()
    stage_text = (stage_text or "").strip()

    if not business_text and not stage_text:
        return (business_text, stage_text)

    llm = _get_groq()
    if not llm:
        return (business_text, stage_text)

    from langchain_core.prompts import ChatPromptTemplate

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a legal text formatter. Fix capitalization, punctuation, and obvious typos in court records.
RULES:
1. Do NOT change any factual content, case numbers, dates, names, legal terms, section numbers, or abbreviations.
2. Keep all acronyms exactly as they appear (DHR, IA, KMC, CrPC, IPC, etc.).
3. Only fix: capitalization (first letter of sentences, proper nouns), punctuation (missing periods, commas), extra spaces, and clear typos.
4. If the text is in ALL CAPS, convert to normal sentence case while preserving proper nouns and acronyms.
5. Return a JSON object with keys "business" and "stage"."""),
        ("human", '{{"business": "{business}", "stage": "{stage}"}}'),
    ])

    from langchain_core.output_parsers import JsonOutputParser
    parser = JsonOutputParser()

    retries = len(GROQ_API_KEYS) + 1
    for attempt in range(retries):
        try:
            chain = prompt | llm | parser
            _wait_groq()
            _maybe_time_rotate_groq()
            result = chain.invoke({"business": business_text, "stage": stage_text})
            cleaned_biz = (result.get("business") or business_text).strip()
            cleaned_stage = (result.get("stage") or stage_text).strip()
            return (cleaned_biz, cleaned_stage)
        except Exception as e:
            estr = str(e)
            if "429" in estr or "Too Many Requests" in estr or "rate_limit_exceeded" in estr:
                if attempt >= retries - 1:
                    break
                _handle_groq_429(e)
                llm = _get_groq()
                if not llm:
                    break
            else:
                break
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
