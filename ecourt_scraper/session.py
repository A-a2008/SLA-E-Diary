"""Reusable eCourts session and parsing logic.

This module provides the EcourtSession class for interacting with
services.ecourts.gov.in, plus HTML parsing utilities.
"""

import json
import logging
import os
import random
import re
import time

logger = logging.getLogger(__name__)

from playwright.sync_api import sync_playwright

# ---------- config ----------
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
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
        logger.info(f"  ⏳ Rate limit: waiting {sleep_for:.0f}s before next eCourts request...")
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


# ---------- OCR (re-export from ocr.py for backward compat) ----------

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


# ---------- HTML helpers ----------

def _clean(val: str) -> str:
    return val.replace("&nbsp;", " ").replace("&amp;", "&").strip()


def _strip_html(val: str) -> str:
    return _clean(re.sub(r"<[^>]+>", "", val))


# ---------- human-like browsing utilities ----------

def _rand(min_ms: int, max_ms: int) -> float:
    return random.uniform(min_ms / 1000, max_ms / 1000)


def _human_type(page, selector: str, text: str, fast: bool = False):
    """Type text with human-like delays between characters."""
    el = page.locator(selector)
    el.wait_for(state="visible", timeout=10000)
    el.click()
    time.sleep(_rand(100, 300))
    el.fill("")
    for char in text:
        delay = _rand(30, 80) if fast else _rand(60, 200)
        el.type(char, delay=delay)
        if not fast and random.random() < 0.03:
            time.sleep(_rand(200, 600))


def _human_click(page, selector_or_el):
    """Move mouse to element with human-like jitter and click."""
    el = page.query_selector(selector_or_el) if isinstance(selector_or_el, str) else selector_or_el
    if not el:
        return el
    box = el.bounding_box()
    if not box:
        el.click()
        return el

    target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

    from_x = random.uniform(100, 400)
    from_y = random.uniform(100, 400)
    steps = random.randint(10, 18)

    for i in range(steps):
        t = (i + 1) / steps
        eased = 1 - (1 - t) ** 2
        x = from_x + (target_x - from_x) * eased + random.uniform(-4, 4)
        y = from_y + (target_y - from_y) * eased + random.uniform(-4, 4)
        page.mouse.move(x, y)
        time.sleep(_rand(8, 25))

    time.sleep(_rand(80, 200))
    page.mouse.click(target_x, target_y)
    return el


def _create_stealth_page(browser):
    """Create a browser context with stealth settings to avoid detection."""
    context = browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        locale="en-IN",
        timezone_id="Asia/Kolkata",
    )
    page = context.new_page()
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        window.chrome = { runtime: {} };
    """)
    return page


def _check_rate_limited(page) -> bool:
    """Check if the page shows a rate-limit / block message."""
    text = page.inner_text("body")
    return "Welcome User" in text or "Search Page not Found" in text or "not Found here" in text


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
        self.browser = self.pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        self.page = _create_stealth_page(self.browser)
        self._casetype_list = None
        self._ecourts_available = True
        self._case_history = []

    @property
    def ecourts_available(self) -> bool:
        return self._ecourts_available

    def _solve_and_search(self, cnr: str, attempt: int = 1) -> dict:
        """Load homepage, type CNR like human, wait for captcha, solve, click search. Returns API JSON."""
        _wait_for_ecourts_slot()
        self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(_rand(2000, 3500))

        if _check_rate_limited(self.page):
            raise RuntimeError("Rate limited by eCourts — IP banned")

        cino_input = self.page.locator("#cino, input[name='cino'], input[id*='cino']")
        cino_input.wait_for(state="visible", timeout=30000)

        self.page.evaluate("""() => {
            const img = document.getElementById('captcha_image');
            window._capSrc = img ? img.getAttribute('src') : '';
        }""")

        for char in cnr:
            cino_input.type(char, delay=random.randint(80, 220))
        time.sleep(_rand(800, 1500))

        self.page.wait_for_selector("#captcha_image", timeout=10000)

        for _ in range(20):
            changed = self.page.evaluate("""() => {
                const img = document.getElementById('captcha_image');
                return img && img.getAttribute('src') !== window._capSrc;
            }""")
            if changed:
                logger.info("  Captcha refreshed after CNR")
                break
            self.page.wait_for_timeout(500)

        self.page.wait_for_function(
            "() => document.getElementById('captcha_image').naturalWidth > 0",
            timeout=10000,
        )
        time.sleep(_rand(1500, 2500))

        from .ocr import solve_captcha

        captcha_text = None
        for snap in range(1, 4):
            captcha_png = self.page.locator("#captcha_image").screenshot()
            debug_name = f"{cnr}_attempt_{attempt}_snap_{snap}"
            captcha_text, raw = solve_captcha(captcha_png, debug_name)
            if captcha_text:
                captcha_text = captcha_text.strip()
                if len(captcha_text) >= 5:
                    break
                logger.info(f"  OCR too short ({len(captcha_text)} chars), retaking screenshot...")
            else:
                logger.info(f"  OCR empty, retaking screenshot...")
            time.sleep(_rand(800, 1500))

        if not captcha_text or len(captcha_text.strip()) < 5:
            return None

        logger.info(f"  OCR: {captcha_text}")
        captcha_input = self.page.locator("#fcaptcha_code")
        captcha_input.wait_for(state="visible", timeout=5000)
        for char in captcha_text:
            captcha_input.type(char, delay=random.randint(60, 180))
        time.sleep(_rand(400, 800))

        if not can_call():
            raise RuntimeError("eCourts API call limit reached")
        with self.page.expect_response(
            lambda r: "cnr_status/searchByCNR" in r.url, timeout=30000
        ) as resp_info:
            _human_click(self.page, "#searchbtn")
            response = resp_info.value
        _LAST_ECOURTS_REQUEST = _time.monotonic()

        return json.loads(response.text())

    def search_case(self, cnr: str) -> dict:
        """Search for a CNR and return parsed case details."""
        from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError
        import json as _json

        for attempt in range(1, MAX_RETRIES + 1):
            logger.info(f"  [{attempt}/{MAX_RETRIES}] Loading eCourts ...")
            try:
                data = self._solve_and_search(cnr, attempt)
            except (TimeoutError, PlaywrightTimeoutError) as e:
                logger.info(f"  Page load timeout: {e}, retrying...")
                time.sleep(_rand(3000, 5000))
                continue
            except _json.JSONDecodeError as e:
                logger.info(f"  Empty API response: {e}, retrying...")
                time.sleep(_rand(3000, 5000))
                continue
            except RuntimeError as e:
                if "Rate limited" in str(e):
                    raise
                logger.info(f"  Error: {e}, retrying...")
                time.sleep(_rand(3000, 5000))
                continue

            if data is None:
                logger.info("  OCR empty, retrying...")
                continue

            if data.get("status") == 1:
                self._casetype_list = data["casetype_list"]
                details = parse_case_details(data["casetype_list"])
                self._case_history = details.get("case_history", [])
                # Check if links are clickable (family matters show plain text)
                if not re.search(r'onclick=viewBusiness\(', data["casetype_list"]):
                    self._ecourts_available = False
                return details

            logger.info(f"  Captcha wrong, retrying...")
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
            lambda r: "viewBusiness" in r.url, timeout=30000
        ) as resp_info:
            self.page.evaluate(f"viewBusiness({args})")
            response = resp_info.value
        time.sleep(_rand(300, 600))

        raw = json.loads(response.text())
        html = raw.get("data_list", "")
        return parse_business_details(html)

    def back_to_history(self):
        """Go back from business details to case history."""
        self.page.evaluate("back_fun('cnr')")
        time.sleep(_rand(1000, 2000))

    def close(self):
        self.browser.close()
        self.pw.stop()
