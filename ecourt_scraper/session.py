"""Reusable eCourts session and parsing logic.

This module provides the EcourtSession class for interacting with
services.ecourts.gov.in, plus HTML parsing utilities.
"""

import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

# ---------- config ----------
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "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")
NVIDIA_OCR_URL = "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v2"
BASE_URL = "https://services.ecourts.gov.in/ecourtindia_v6/"
MAX_RETRIES = 10

# call-limit counter shared across sessions
_CALL_COUNTER = 0
ECOURTS_CALL_LIMIT = 0  # 0 = no limit

# --- eCourts rate limiter (1 req/min) ---
import time as _time
_LAST_ECOURTS_REQUEST = 0.0
ECOURTS_MIN_INTERVAL = 20.0  # seconds between requests

def _wait_for_ecourts_slot():
    """Block until at least ECOURTS_MIN_INTERVAL has passed since last eCourts request."""
    global _LAST_ECOURTS_REQUEST
    now = _time.monotonic()
    elapsed = now - _LAST_ECOURTS_REQUEST
    if elapsed < ECOURTS_MIN_INTERVAL:
        sleep_for = ECOURTS_MIN_INTERVAL - elapsed
        print(f"  ⏳ Rate limit: waiting {sleep_for:.0f}s before next eCourts request...", file=sys.stderr)
        _time.sleep(sleep_for)
    _LAST_ECOURTS_REQUEST = _time.monotonic()


def set_call_limit(n: int):
    global ECOURTS_CALL_LIMIT
    ECOURTS_CALL_LIMIT = n


def can_call() -> bool:
    global _CALL_COUNTER
    if ECOURTS_CALL_LIMIT > 0 and _CALL_COUNTER >= ECOURTS_CALL_LIMIT:
        return False
    _CALL_COUNTER += 1
    return True


def reset_call_counter():
    global _CALL_COUNTER
    _CALL_COUNTER = 0


# ---------- OCR ----------

def _extract_ocr_text(raw: dict) -> str:
    try:
        parts = []
        for det in raw["data"][0]["text_detections"]:
            t = det["text_prediction"]["text"]
            if t and t not in ("-", "—", ""):
                parts.append(t)
        if parts:
            return max(parts, key=len)
    except (KeyError, TypeError, IndexError):
        pass
    try:
        return raw["choices"][0]["message"]["content"]
    except (KeyError, TypeError, IndexError):
        pass
    try:
        return raw["text"]
    except KeyError:
        pass
    try:
        return raw["result"]
    except KeyError:
        pass
    return ""


def solve_captcha(image_bytes: bytes) -> str:
    import base64
    import requests
    from .nvidia_rate_limiter import wait as nvidia_wait

    image_b64 = base64.b64encode(image_bytes).decode()
    if len(image_b64) >= 180_000:
        return ""

    nvidia_wait()
    resp = requests.post(
        NVIDIA_OCR_URL,
        headers={
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Accept": "application/json",
        },
        json={
            "input": [{"type": "image_url", "url": f"data:image/png;base64,{image_b64}"}]
        },
        timeout=30,
    )
    resp.raise_for_status()
    return _extract_ocr_text(resp.json()).strip()


# ---------- HTML helpers ----------

def _clean(val: str) -> str:
    return val.replace("&nbsp;", " ").replace("&amp;", "&").strip()


def _strip_html(val: str) -> str:
    return _clean(re.sub(r"<[^>]+>", "", val))


# ---------- HTML parsers ----------

def parse_case_details(casetype_list: str) -> dict:
    """Parse casetype_list HTML into structured case details."""
    result = {
        "registration_number": None,
        "petitioner_advocate": [],
        "respondent_advocate": [],
        "case_history": [],
    }

    m = re.search(
        r"Registration\s*(?:Number|No|\.)\s*</t[dh]>\s*<t[dh][^>]*>\s*([\w/\-\.]+)",
        casetype_list, re.IGNORECASE
    )
    if m:
        result["registration_number"] = _clean(m.group(1))

    def _parse_parties(section_text: str) -> list[dict]:
        entries = []
        parts = re.split(r"\d+\)\s*", section_text.strip())
        for part in parts:
            if not part.strip():
                continue
            part = _strip_html(part)
            part = re.sub(r"\s+", " ", part).strip()
            adv_match = re.search(r"Advocate[-\s]*(.+?)$", part, re.IGNORECASE)
            if adv_match:
                party = part[: adv_match.start()].strip().rstrip(" ,")
                adv = adv_match.group(1).strip().rstrip(" ,")
            else:
                party = part.strip().rstrip(" ,")
                adv = ""
            if party:
                entry = {"party": party}
                if adv:
                    entry["advocate"] = adv
                entries.append(entry)
        return entries

    pet_section = re.search(
        r"Petitioner\s*and\s*Advocate</h3>(.*?)</ul>",
        casetype_list, re.IGNORECASE | re.DOTALL
    )
    if pet_section:
        result["petitioner_advocate"] = _parse_parties(pet_section.group(1))

    resp_section = re.search(
        r"Respondent\s*and\s*Advocate</h3>(.*?)</ul>",
        casetype_list, re.IGNORECASE | re.DOTALL
    )
    if resp_section:
        result["respondent_advocate"] = _parse_parties(resp_section.group(1))

    history_section = re.search(
        r"Case History</h3>(.*?)(?:<h3|$)",
        casetype_list, re.IGNORECASE | re.DOTALL
    )
    if history_section:
        rows = re.findall(r"<tr>(.*?)</tr>", history_section.group(1), re.DOTALL)
        for row in rows:
            cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
            if len(cells) >= 4:
                judge = _strip_html(cells[0])
                business_date = _strip_html(cells[1])
                hearing_date = _strip_html(cells[2])
                purpose = _strip_html(cells[3])
                if judge and hearing_date:
                    result["case_history"].append({
                        "judge": judge,
                        "business_date": business_date,
                        "hearing_date": hearing_date,
                        "purpose": purpose,
                    })

    return result


def extract_business_links(casetype_list: str) -> list[dict]:
    """Extract business date links from case history."""
    links = re.findall(r'onclick=viewBusiness\(([^)]+)\)', casetype_list)
    result = []
    for link in links:
        args = [a.strip().strip("'\"") for a in link.split(",")]
        if len(args) >= 7:
            result.append({
                "court_code": args[0],
                "dist_code": args[1],
                "nextdate": args[2],
                "case_number": args[3],
                "state_code": args[4],
                "business_status": args[5],
                "business_date": args[6],
                "court_no": args[7] if len(args) > 7 else "",
                "national_court_code": args[8] if len(args) > 8 else "",
                "search_by": args[9] if len(args) > 9 else "cnr",
                "srno": args[10] if len(args) > 10 else "",
            })
    return result


def parse_business_details(html: str) -> dict:
    """Parse the business details HTML shown after clicking a date link."""
    d = {}
    m = re.search(
        r"<b>Business</b>\s*</td>\s*<td[^>]*>:\s*</td>\s*<td[^>]*>(.*?)</td>",
        html, re.DOTALL
    )
    if m:
        d["business"] = _strip_html(m.group(1))

    m = re.search(
        r"<b>Next Purpose</b>\s*</td>\s*<td[^>]*>:\s*</td>\s*<td[^>]*>(.*?)</td>",
        html, re.DOTALL
    )
    if m:
        d["next_purpose"] = _strip_html(m.group(1))

    m = re.search(
        r"<b>Next Hearing Date</b>\s*</td>\s*<td[^>]*>:\s*</td>\s*<td[^>]*>(.*?)</td>",
        html, re.DOTALL
    )
    if m:
        d["next_hearing_date"] = _strip_html(m.group(1))

    m = re.search(r"<b>Date\s*</b>\s*:\s*([^<]+)", html)
    if m:
        d["date"] = _clean(m.group(1))

    return d


# ---------- browser session ----------

class EcourtSession:
    """Manages a Playwright session for eCourts.gov.in."""

    def __init__(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=True)
        self.page = self.browser.new_page()
        self.page.set_viewport_size({"width": 1280, "height": 900})
        self._casetype_list = None
        self._ecourts_available = True
        self._case_history = []

    @property
    def ecourts_available(self) -> bool:
        return self._ecourts_available

    def _solve_and_search(self, cnr: str) -> dict:
        """Load homepage, fill CNR, solve captcha, click search. Returns API JSON."""
        _wait_for_ecourts_slot()
        self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_timeout(3000)

        self.page.evaluate(f"document.getElementById('cino').value = '{cnr}'")

        self.page.wait_for_selector("#captcha_image", timeout=5000)
        captcha_png = self.page.locator("#captcha_image").screenshot()
        captcha_text = solve_captcha(captcha_png)

        if not captcha_text:
            return None

        print(f"  OCR: {captcha_text}", file=sys.stderr)
        self.page.evaluate(f"document.getElementById('fcaptcha_code').value = '{captcha_text}'")

        if not can_call():
            raise RuntimeError("eCourts API call limit reached")
        _wait_for_ecourts_slot()
        with self.page.expect_response(
            lambda r: "cnr_status/searchByCNR" in r.url, timeout=20000
        ) as resp_info:
            self.page.evaluate("document.getElementById('searchbtn').click()")
            response = resp_info.value

        return json.loads(response.text())

    def search_case(self, cnr: str) -> dict:
        """Search for a CNR and return parsed case details."""
        for attempt in range(1, MAX_RETRIES + 1):
            print(f"  [{attempt}/{MAX_RETRIES}] Loading eCourts ...", file=sys.stderr)
            data = self._solve_and_search(cnr)

            if data is None:
                print("  OCR empty, retrying...", file=sys.stderr)
                continue

            if data.get("status") == 1:
                self._casetype_list = data["casetype_list"]
                details = parse_case_details(data["casetype_list"])
                self._case_history = details.get("case_history", [])
                # Check if links are clickable (family matters show plain text)
                if not re.search(r'onclick=viewBusiness\(', data["casetype_list"]):
                    self._ecourts_available = False
                return details

            print(f"  Captcha wrong, retrying...", file=sys.stderr)
            time.sleep(1)

        raise RuntimeError(f"Failed after {MAX_RETRIES} attempts")

    def get_business_links(self) -> list[dict]:
        """Return the list of business date links from the search result."""
        if not self._casetype_list:
            raise RuntimeError("No search result loaded. Call search_case() first.")
        return extract_business_links(self._casetype_list)

    def get_purpose_hearings(self) -> list[dict]:
        """Return case history rows as business-like items (for non-clickable cases).

        Each item: {business_date: date, purpose: str, hearing_date: date}
        The purpose text is used as both business and stage content.
        """
        items = []
        for row in self._case_history:
            biz_date = row.get("business_date", "").strip()
            purpose = row.get("purpose", "").strip()
            hearing_date = row.get("hearing_date", "").strip()
            if purpose and biz_date:
                items.append({
                    "business_date": biz_date,
                    "purpose": purpose,
                    "hearing_date": hearing_date,
                })
        return items

    def view_business(self, link: dict) -> dict:
        """Click a business date link and return the parsed business details."""
        if not can_call():
            raise RuntimeError("eCourts call limit reached")
        _wait_for_ecourts_slot()
        args = (
            f"'{link['court_code']}','{link['dist_code']}','{link['nextdate']}',"
            f"'{link['case_number']}','{link['state_code']}','{link['business_status']}',"
            f"'{link['business_date']}','{link['court_no']}','{link['national_court_code']}',"
            f"'{link['search_by']}','{link['srno']}'"
        )
        with self.page.expect_response(
            lambda r: "home/viewBusiness" in r.url, timeout=20000
        ) as resp_info:
            self.page.evaluate(f"viewBusiness({args})")
            response = resp_info.value

        raw = json.loads(response.text())
        html = raw.get("data_list", "")
        return parse_business_details(html)

    def back_to_history(self):
        """Go back from business details to case history."""
        self.page.evaluate("back_fun('cnr')")
        time.sleep(1)

    def close(self):
        self.browser.close()
        self.pw.stop()
