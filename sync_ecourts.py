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
        EcourtSession, set_call_limit, reset_call_counter, can_call,
    )

    reset_call_counter()
    set_call_limit(ECOURTS_CALL_LIMIT)

    result = {"items": [], "ecourts_available": True, "error": None, "total_available": 0}
    skip_dates = skip_dates or set()

    session = EcourtSession()
    try:
        details = session.search_case(cnr)

        if not session.ecourts_available:
            result["ecourts_available"] = False
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

    while True:
        # Fetch pending + recheck cases
        data = api_get('/api/ecourts/pending/')
        cases = data.get('cases', [])
        pending_total = data.get('pending_total', 0)

        if not cases:
            logger.info(f"No pending or recheck cases ({pending_total} pending remain if any). Done.")
            break

        logger.info(f"Processing {len(cases)} cases ({pending_total} pending total in queue)")

        for case_data in cases:
            case_id = case_data['id']
            cnr = case_data['cnr']
            label = f"{case_data['case_type']} {case_data['case_number']}/{case_data['case_year']}"
            skip_dates = set(case_data.get('already_fetched_dates', []))

            logger.info(f"  [{case_id}] {label} (CNR: {cnr}) — scraping...")

            try:
                scraped = scrape_case(cnr, skip_dates=skip_dates)

                if scraped.get('error'):
                    logger.error(f"  [{case_id}] Scraper error: {scraped['error']}")
                    # Still push with error status so PA marks it
                    payload = {
                        'case_id': case_id,
                        'status': 'pending',
                        'entries': [],
                        'ecourts_available': scraped.get('ecourts_available', True),
                    }
                    api_post('/api/ecourts/upsert/', payload)
                    continue

                if not scraped['ecourts_available']:
                    logger.info(f"  [{case_id}] Unsupported case type (no clickable links)")
                    api_post('/api/ecourts/upsert/', {
                        'case_id': case_id,
                        'status': 'unsupported',
                        'entries': [],
                        'ecourts_available': False,
                    })
                    total_processed += 1
                    continue

                items = scraped.get('items', [])
                if not items:
                    logger.info(f"  [{case_id}] No new entries found")
                    api_post('/api/ecourts/upsert/', {
                        'case_id': case_id,
                        'status': 'done',
                        'entries': [],
                        'ecourts_available': True,
                    })
                    total_processed += 1
                    continue

                logger.info(f"  [{case_id}] Pushing {len(items)} entries...")
                result = api_post('/api/ecourts/upsert/', {
                    'case_id': case_id,
                    'status': 'done',
                    'entries': items,
                    'ecourts_available': True,
                })
                logger.info(f"  [{case_id}] Done — created {result.get('created', 0)}, updated {result.get('updated', 0)}")
                total_processed += 1

                # Small delay between cases
                time.sleep(2)

            except Exception as e:
                logger.exception(f"  [{case_id}] Failed: {e}")
                continue

        # Brief pause before next batch
        time.sleep(1)

    logger.info(f"Sync complete. Processed {total_processed} cases.")


if __name__ == '__main__':
    main()
