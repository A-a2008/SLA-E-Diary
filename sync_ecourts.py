#!/usr/bin/env python3
"""
sync_ecourts.py — Laptop-side eCourts sync script.

Fetches pending cases from the PythonAnywhere-hosted site,
runs the Playwright scraper locally, and pushes results back.

Usage:
    export API_TOKEN=your_shared_secret
    export PA_URL=https://yourusername.pythonanywhere.com
    python sync_ecourts.py

The script loops until all pending cases are processed.
"""

import os
import sys
import time
import json
import logging

from dotenv import load_dotenv
import requests

# Add project root for ecourt_scraper.session
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from ecourt_scraper.session import (
    EcourtSession,
    set_call_limit,
    reset_call_counter,
)

load_dotenv(os.path.join(_project_root, 'E_Diary', '.env'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)

API_TOKEN = os.getenv('API_TOKEN')
PA_URL = os.getenv('PA_URL') or os.getenv('API_BASE_URL', 'http://localhost:8099')

ECOURTS_CALL_LIMIT = 200
HEADERS = {'Authorization': f'Bearer {API_TOKEN}'} if API_TOKEN else {}


# ============================================================
# API helpers
# ============================================================

def api_get(endpoint: str) -> dict:
    url = f'{PA_URL}{endpoint}'
    params = {}
    if not API_TOKEN:
        params['token'] = ''
    resp = requests.get(url, headers=HEADERS, params=params if params else None, timeout=30)
    resp.raise_for_status()
    return resp.json()


def api_post(endpoint: str, data: dict) -> dict:
    url = f'{PA_URL}{endpoint}'
    resp = requests.post(url, headers={**HEADERS, 'Content-Type': 'application/json'},
                         json=data, timeout=60)
    resp.raise_for_status()
    return resp.json()


# ============================================================
# Scraper wrapper (no Django ORM)
# ============================================================

def _parse_date_dmy(date_str: str):
    if not date_str:
        return None
    import datetime
    for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def scrape_case(cnr: str, skip_dates: set = None) -> dict:
    """Run the Playwright scraper locally. Returns dict with items, ecourts_available, error."""
    from ecourt_scraper.session import (
        EcourtSession, set_call_limit, reset_call_counter,
    )

    reset_call_counter()
    set_call_limit(ECOURTS_CALL_LIMIT)

    result = {"items": [], "ecourts_available": True, "error": None, "total_available": 0}
    skip_dates = skip_dates or set()

    session = EcourtSession()
    try:
        details = session.search_case(cnr)

        if not session.ecourts_available:
            # Non-clickable case — fall back to purpose-of-hearing from case_history
            purpose_items = session.get_purpose_hearings()
            for item in purpose_items:
                biz_date = _parse_date_dmy(item.get("business_date"))
                if not biz_date:
                    continue
                date_str = biz_date.strftime('%d-%m-%Y')
                if date_str in skip_dates:
                    continue
                result["items"].append({
                    "previous_date": biz_date.strftime('%Y-%m-%d'),
                    "business": item.get("purpose", ""),
                    "next_hearing": item.get("hearing_date") or None,
                    "stage": item.get("purpose", ""),
                })
            result["total_available"] = len(purpose_items)
            result["ecourts_available"] = bool(purpose_items)
            return result

        links = session.get_business_links()
        result["total_available"] = len(links)

        for i, link in enumerate(links):
            link_date = (link.get("business_date") or "").strip()

            if link_date in skip_dates:
                continue

            try:
                biz = session.view_business(link)
            except RuntimeError as e:
                if "limit" in str(e).lower():
                    logger.warning(f"  Call limit reached after {i} items")
                else:
                    result["error"] = str(e)
                break

            biz_date = _parse_date_dmy(link.get("business_date"))
            if not biz_date:
                biz_date = _parse_date_dmy(biz.get("date"))

            biz_text = biz.get("business", "").strip()
            if not biz_text:
                continue

            next_hearing = _parse_date_dmy(biz.get("next_hearing_date"))
            stage = biz.get("next_purpose", "").strip()

            result["items"].append({
                "previous_date": biz_date.strftime('%Y-%m-%d') if biz_date else None,
                "business": biz_text,
                "next_hearing": next_hearing.strftime('%Y-%m-%d') if next_hearing else None,
                "stage": stage,
            })

            if i < len(links) - 1:
                session.back_to_history()

    except RuntimeError as e:
        result["error"] = str(e)
    except Exception as e:
        result["error"] = str(e)
        logger.exception("Scraper error")
    finally:
        session.close()

    return result


# ============================================================
# Rate limiter (26 req/min program-wide)
# ============================================================

import threading

_rate_lock = threading.Lock()
_last_call_time: float = 0
_MIN_INTERVAL = 60.0 / 26  # ~2.3077 seconds between Groq calls


def _wait_for_rate_limit():
    """Block until the minimum interval since the last Groq API call has elapsed."""
    global _last_call_time
    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_call_time
        if elapsed < _MIN_INTERVAL:
            sleep_for = _MIN_INTERVAL - elapsed
            time.sleep(sleep_for)
        _last_call_time = time.monotonic()


# ============================================================
# Groq text cleanup (runs on laptop to save PA compute)
# ============================================================

GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_FALLBACK_MODEL = "openai/gpt-oss-20b"
GROQ_FINAL_FALLBACK = "qwen/qwen3-32b"


def _get_groq():
    from langchain_groq import ChatGroq

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
            _wait_for_rate_limit()
            chain.invoke({})
            return llm
        except Exception:
            continue
    return None


def cleanup_texts(items: list) -> list:
    """Pass scraped items through Groq to fix caps, punctuation, typos."""
    llm = _get_groq()
    if not llm:
        return items

    from langchain_core.prompts import ChatPromptTemplate

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a legal text formatter. Fix capitalization, punctuation, and obvious typos in court records.
RULES:
1. Do NOT change any factual content, case numbers, dates, names, legal terms, section numbers, or abbreviations.
2. Keep all acronyms exactly as they appear (DHR, IA, KMC, CrPC, IPC, etc.).
3. Only fix: capitalization (first letter of sentences, proper nouns), punctuation (missing periods, commas), extra spaces, and clear typos.
4. If text is in ALL CAPS, convert to normal sentence case while preserving proper nouns and acronyms.
5. Return a JSON object with keys "business" and "stage"."""),
        ("human", '{{"business": "{business}", "stage": "{stage}"}}'),
    ])

    from langchain_core.output_parsers import JsonOutputParser
    parser = JsonOutputParser()
    chain = prompt | llm | parser

    cleaned = []
    for item in items:
        try:
            _wait_for_rate_limit()
            result = chain.invoke({
                "business": item.get("business", ""),
                "stage": item.get("stage", ""),
            })
            item["business"] = (result.get("business") or item["business"]).strip()
            item["stage"] = (result.get("stage") or item["stage"]).strip()
        except Exception:
            pass
        cleaned.append(item)
    return cleaned


# ============================================================
# Main loop
# ============================================================

def main():
    if not API_TOKEN:
        logger.warning("API_TOKEN not set — using unauthenticated requests")
    if not PA_URL:
        logger.error("PA_URL not set. Export PA_URL=https://yourusername.pythonanywhere.com")
        sys.exit(1)

    logger.info(f"Starting eCourts sync against {PA_URL}")
    total_processed = 0

    def _process_phase(label_phase: str, cases: list):
        nonlocal total_processed
        if not cases:
            logger.info(f"  [{label_phase}] No cases to process")
            return

        for case_data in cases:
            case_id = case_data['id']
            cnr = case_data['cnr']
            label = f"{case_data['case_type']} {case_data['case_number']}/{case_data['case_year']}"
            skip_dates = set(case_data.get('already_fetched_dates', []))

            logger.info(f"  [{label_phase}][{case_id}] {label} (CNR: {cnr}) — scraping...")

            try:
                scraped = scrape_case(cnr, skip_dates=skip_dates)

                if scraped.get('error'):
                    logger.error(f"  [{label_phase}][{case_id}] Scraper error: {scraped['error']}")
                    payload = {
                        'case_id': case_id,
                        'status': 'pending' if label_phase == 'PENDING' else 'done',
                        'entries': [],
                        'ecourts_available': scraped.get('ecourts_available', True),
                    }
                    api_post('/api/ecourts/upsert/', payload)
                    continue

                ecourts_available = scraped.get('ecourts_available', True)
                items = scraped.get('items', [])

                if items:
                    logger.info(f"  [{label_phase}][{case_id}] Cleaning {len(items)} entries with Groq...")
                    items = cleanup_texts(items)

                status = 'done' if items or ecourts_available else 'no_data'

                logger.info(f"  [{label_phase}][{case_id}] Pushing {len(items)} entries (status={status})...")
                result = api_post('/api/ecourts/upsert/', {
                    'case_id': case_id,
                    'status': status,
                    'entries': items,
                    'ecourts_available': ecourts_available,
                })
                logger.info(f"  [{label_phase}][{case_id}] Done — created {result.get('created', 0)}, updated {result.get('updated', 0)}")
                total_processed += 1

                time.sleep(2)

            except Exception as e:
                logger.exception(f"  [{label_phase}][{case_id}] Failed: {e}")
                continue

    while True:
        data = api_get('/api/ecourts/pending/')
        pending = data.get('pending', [])
        refresh = data.get('refresh', [])
        pending_total = data.get('pending_total', 0)
        refresh_total = data.get('refresh_total', 0)

        if not pending and not refresh:
            logger.info(f"Nothing to process (pending={pending_total}, refresh={refresh_total}). Done.")
            break

        logger.info(f"Pending: {len(pending)} | Refresh: {len(refresh)} (queue totals — pending: {pending_total}, refresh: {refresh_total})")

        # Phase 1 — process pending cases (full history scrape)
        _process_phase('PENDING', pending)

        # Phase 2 — process refresh cases (hearing dates already passed)
        _process_phase('REFRESH', refresh)

        time.sleep(1)

    logger.info(f"Sync complete. Processed {total_processed} cases.")


if __name__ == '__main__':
    main()
