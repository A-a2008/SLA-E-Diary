import re
import json
from playwright.sync_api import sync_playwright, Page, Browser

from .config import BASE_URL


SELECTORS = {
    "cnr_input": "#cino",
    "captcha_image": "#captcha_image",
    "captcha_input": "#fcaptcha_code",
    "search_button": "#searchbtn",
    "error_modal": "#validateError",
}


class EcourtBrowser:
    """Manages a headless Chromium session for the eCourts portal."""

    def __init__(self):
        self.playwright = None
        self.browser: Browser | None = None
        self.page: Page | None = None
        self._api_response_json = None

    def start(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.page = self.browser.new_page()
        self.page.set_viewport_size({"width": 1280, "height": 900})

    def navigate_to_homepage(self):
        print("  🌐 Navigating to eCourts homepage ...")
        self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        self.page.wait_for_timeout(3000)

    def enter_cnr(self, cnr_text: str):
        locator = self.page.locator(SELECTORS["cnr_input"]).first
        locator.wait_for(state="attached", timeout=10000)
        try:
            locator.fill(cnr_text)
        except Exception:
            self.page.evaluate(f"document.getElementById('cino').value = '{cnr_text}'")
        print(f"  ✅ CNR entered: {cnr_text}")

    def fetch_captcha_image(self) -> bytes:
        """Screenshot the captcha image element to read exactly what the browser displays."""
        locator = self.page.locator(SELECTORS["captcha_image"]).first
        locator.wait_for(state="attached", timeout=5000)
        return locator.screenshot()

    def enter_captcha(self, captcha_text: str):
        locator = self.page.locator(SELECTORS["captcha_input"]).first
        locator.wait_for(state="attached", timeout=5000)
        try:
            locator.fill(captcha_text)
        except Exception:
            # Fallback: use JS to set value directly
            self.page.evaluate(f"document.getElementById('fcaptcha_code').value = '{captcha_text}'")
        print(f"  🔤 Captcha entered: {captcha_text}")

    def click_search(self):
        with self.page.expect_response(
            lambda r: "cnr_status/searchByCNR" in r.url,
            timeout=20000,
        ) as resp_info:
            self.page.evaluate("document.getElementById('searchbtn').click()")
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
