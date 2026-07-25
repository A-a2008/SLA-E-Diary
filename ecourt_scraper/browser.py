import json
import random
import re
import time

from playwright.sync_api import sync_playwright, Page, Browser

from .config import BASE_URL


SELECTORS = {
    "cnr_input": "#cino",
    "captcha_image": "#captcha_image",
    "captcha_input": "#fcaptcha_code",
    "search_button": "#searchbtn",
    "error_modal": "#validateError",
}


def _rand(min_ms: int, max_ms: int) -> float:
    return random.uniform(min_ms / 1000, max_ms / 1000)


def _human_click(page, selector_or_el):
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


class EcourtBrowser:
    """Manages a headless Chromium session for the eCourts portal."""

    def __init__(self):
        self.playwright = None
        self.browser: Browser | None = None
        self.page: Page | None = None
        self._api_response_json = None

    def start(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = self.browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        self.page = context.new_page()
        self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            window.chrome = { runtime: {} };
        """)

    def navigate_to_homepage(self):
        print("  🌐 Navigating to eCourts homepage ...")
        self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(_rand(2500, 4000))
        text = self.page.inner_text("body")
        if "Welcome User" in text or "Search Page not Found" in text:
            print("  ⚠️  Rate limited by eCourts (Welcome User page)")
            raise RuntimeError("Rate limited by eCourts — IP banned")

    def enter_cnr(self, cnr_text: str):
        locator = self.page.locator(SELECTORS["cnr_input"]).first
        locator.wait_for(state="attached", timeout=10000)
        locator.click()
        time.sleep(_rand(100, 300))
        for char in cnr_text:
            locator.type(char, delay=random.randint(80, 220))
        print(f"  ✅ CNR entered: {cnr_text}")
        time.sleep(_rand(800, 1500))

    def fetch_captcha_image(self) -> bytes:
        """Screenshot the captcha image element to read exactly what the browser displays."""
        locator = self.page.locator(SELECTORS["captcha_image"]).first
        locator.wait_for(state="attached", timeout=5000)
        return locator.screenshot()

    def enter_captcha(self, captcha_text: str):
        locator = self.page.locator(SELECTORS["captcha_input"]).first
        locator.wait_for(state="attached", timeout=5000)
        locator.click()
        time.sleep(_rand(100, 250))
        for char in captcha_text:
            locator.type(char, delay=random.randint(60, 180))
        print(f"  🔤 Captcha entered: {captcha_text}")
        time.sleep(_rand(400, 800))

    def click_search(self):
        with self.page.expect_response(
            lambda r: "cnr_status/searchByCNR" in r.url,
            timeout=30000,
        ) as resp_info:
            _human_click(self.page, "#searchbtn")
            print("  🔍 Search button clicked, waiting for API response ...")
            response = resp_info.value

        status_code = response.status
        body = response.text()
        print(f"  📡 API response received (HTTP {status_code}, {len(body)} chars)")

        try:
            self._api_response_json = json.loads(body)
        except json.JSONDecodeError as e:
            print(f"  ⚠️  Could not parse API response JSON: {e}")
            self._api_response_json = None

    def wait_for_search_complete(self, timeout: int = 20000) -> bool:
        """Check the captured API response. Returns True if status=1."""
        if self._api_response_json is None:
            print("  ⚠️  No API response captured")
            return False

        status = self._api_response_json.get("status")
        if status == 1:
            return True
        else:
            print(f"  ⚠️  API returned status={status!r} (search failed)")
            div_captcha = self._api_response_json.get("div_captcha", "")
            if "Invalid" in div_captcha:
                print("  ⚠️  Reason: Invalid Captcha")
            return False

    def extract_filing_and_registration(self) -> tuple[str | None, str | None]:
        """Extract Filing Number and Registration Number from the API response casetype_list HTML."""
        filing = None
        registration = None

        if not self._api_response_json:
            return None, None

        casetype_list = self._api_response_json.get("casetype_list", "")
        if not casetype_list:
            print("  ⚠️  API response has no casetype_list")
            return None, None

        # Save the response HTML for debugging
        import os
        from .config import CAPTCHA_DEBUG_DIR
        os.makedirs(CAPTCHA_DEBUG_DIR, exist_ok=True)
        with open(os.path.join(CAPTCHA_DEBUG_DIR, "casetype_list.html"), "w") as f:
            f.write(casetype_list)

        # Strategy 1: Regex over the HTML
        def clean(val: str) -> str:
            return val.replace("&nbsp;", " ").replace("&amp;", "&").strip()

        fn_match = re.search(
            r"Filing\s*(?:Number|No|\.)[:\s]*([\w/\-\.\s]+)",
            casetype_list, re.IGNORECASE
        )
        if fn_match:
            filing = clean(fn_match.group(1))

        rn_match = re.search(
            r"Registration\s*(?:Number|No|\.)[:\s]*([\w/\-\.\s]+)",
            casetype_list, re.IGNORECASE
        )
        if rn_match:
            registration = clean(rn_match.group(1))

        # Strategy 2: table row extraction
        if not filing:
            filing = self._extract_from_table(casetype_list, "Filing")
        if not registration:
            registration = self._extract_from_table(casetype_list, "Registration")

        return filing, registration

    def _extract_from_table(self, html: str, label: str) -> str | None:
        """Try to extract value from an HTML table row by label."""
        pattern = re.compile(
            rf'{label}[^<]*</t[dh]>\s*<t[dh][^>]*>([^<]+)',
            re.IGNORECASE | re.DOTALL
        )
        m = pattern.search(html)
        if m:
            val = m.group(1).strip()
            # Clean HTML entities
            val = val.replace("&nbsp;", " ").replace("&amp;", "&").strip()
            return val
        return None

    def close(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
