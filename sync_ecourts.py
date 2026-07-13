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
import argparse
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
                "previous_date": biz_date.strftime('%Y-%m-%d'),
                "business": purpose.title(),
                "next_hearing": item.get("hearing_date") or None,
                "stage": purpose.title(),
            })
        result["total_available"] = len(purpose_items)
        result["ecourts_available"] = bool(purpose_items)

    except RuntimeError as e:
        result["error"] = str(e)
    except Exception as e:
        result["error"] = str(e)
        logger.exception("Scraper error")
    finally:
        session.close()

    return result


# ============================================================
# Rate limiter (15 req/min program-wide, auto-tuned from API)
# ============================================================
from ecourt_scraper.nvidia_rate_limiter import wait as nvidia_wait

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
if not NVIDIA_API_KEY:
    logger.error("NVIDIA_API_KEY not set in .env")
    sys.exit(1)

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODELS = [
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.5-397b-a17b",
]


def _nvidia_chat(model: str, messages: list, temperature: float = 0, max_tokens: int = 4096) -> str | None:
    """Send a chat completion to NVIDIA with rate-limit enforcement. Returns content string or None."""
    from openai import OpenAI
    client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=NVIDIA_API_KEY)
    try:
        nvidia_wait()
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


def cleanup_texts(items: list) -> list:
    """Pass scraped items through NVIDIA LLM to fix caps, punctuation, typos.
    Tries 3 models in order. Falls back to original text if all fail."""
    system = (
        "You are a legal text formatter. Fix capitalization, punctuation, and obvious typos in court records.\n"
        "RULES:\n"
        "1. Do NOT change any factual content, case numbers, dates, names, legal terms, section numbers, or abbreviations.\n"
        "2. Keep all acronyms exactly as they appear (DHR, IA, KMC, CrPC, IPC, etc.).\n"
        "3. Only fix: capitalization (first letter of sentences, proper nouns), punctuation (missing periods, commas), extra spaces, and clear typos.\n"
        "4. If text is in ALL CAPS, convert to normal sentence case while preserving proper nouns and acronyms.\n"
        "5. Return a JSON object with keys \"business\" and \"stage\"."
    )

    for item in items:
        for model in NVIDIA_MODELS:
            user_msg = json.dumps({
                "business": item.get("business", ""),
                "stage": item.get("stage", ""),
            })
            result = _nvidia_chat(model, [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ])
            if result is None:
                continue
            try:
                parsed = json.loads(result)
                biz = parsed.get("business", "")
                stage = parsed.get("stage", "")
                if biz or stage:
                    item["business"] = biz.strip() or item.get("business", "")
                    item["stage"] = stage.strip() or item.get("stage", "")
                    break
            except (json.JSONDecodeError, TypeError):
                continue
        else:
            logger.warning(f"NVIDIA: all models failed for item, keeping original")
    return items


# ============================================================
# Main loop
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Sync eCourts data')
    parser.add_argument('--force', action='store_true',
                        help='Re-scrape ALL cases with CNRs, ignoring status')
    parser.add_argument('--update', action='store_true',
                        help='Only refresh cases whose next hearing date is today or past (skip pending)')
    args = parser.parse_args()

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
                    models_str = " → ".join(NVIDIA_MODELS)
                    logger.info(f"  [{label_phase}][{case_id}] Cleaning {len(items)} entries (models: {models_str})...")
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
        if args.update:
            endpoint = '/api/ecourts/pending/?update=true'
        else:
            endpoint = '/api/ecourts/pending/?force=true' if args.force else '/api/ecourts/pending/'
        data = api_get(endpoint)
        pending = data.get('pending', [])
        refresh = data.get('refresh', [])
        pending_total = data.get('pending_total', 0)
        refresh_total = data.get('refresh_total', 0)

        if args.update:
            logger.info(f"Update mode: Pending: {len(pending)} | Refresh: {len(refresh)} (queue totals — pending: {pending_total}, refresh: {refresh_total})")
        else:
            logger.info(f"Pending: {len(pending)} | Refresh: {len(refresh)} (queue totals — pending: {pending_total}, refresh: {refresh_total})")

        if not pending and not refresh:
            logger.info(f"Nothing to process. Done.")
            break

        if pending:
            _process_phase('PENDING', pending)

        if refresh:
            _process_phase('REFRESH', refresh)

        time.sleep(1)

    logger.info(f"Sync complete. Processed {total_processed} cases.")


if __name__ == '__main__':
    main()
