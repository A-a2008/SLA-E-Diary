import os
import sys
import json
import time

from .config import CNR, MAX_RETRIES, OUTPUT_FILE, CAPTCHA_DEBUG_DIR
from .ocr import solve_captcha
from .browser import EcourtBrowser


def main():
    print("=" * 60)
    print("  eCourts CNR Scraper")
    print(f"  CNR: {CNR}")
    print(f"  Max retries: {MAX_RETRIES}")
    print("=" * 60)
    print()

    filing_number = None
    registration_number = None

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"\n─── Attempt {attempt}/{MAX_RETRIES} ───")

        browser = EcourtBrowser()

        try:
            browser.start()
            browser.navigate_to_homepage()
            browser.enter_cnr(CNR)

            captcha_bytes = browser.fetch_captcha_image()

            captcha_text, _ = solve_captcha(
                captcha_bytes,
                debug_name=f"attempt_{attempt}",
            )
            print(f"  🤖 OCR result: '{captcha_text}'")

            if not captcha_text:
                print("  ❌ OCR returned empty text, retrying ...")
                continue

            browser.enter_captcha(captcha_text)
            browser.click_search()

            success = browser.wait_for_search_complete(timeout=20000)

            if success:
                filing_number, registration_number = browser.extract_filing_and_registration()
                if filing_number or registration_number:
                    break
                print("  ⚠️  Could not extract numbers from response, retrying ...")
            else:
                print("  ❌ Search did not return case data (likely wrong captcha).")

        finally:
            browser.close()
            print("  🏁 Browser closed.")

        time.sleep(2)

    # ── Output ──
    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)
    print(f"  CNR:                {CNR}")

    if filing_number:
        print(f"  Filing Number:      {filing_number}")
    else:
        print("  Filing Number:      NOT FOUND")

    if registration_number:
        print(f"  Registration Number: {registration_number}")
    else:
        print("  Registration Number: NOT FOUND")

    print()

    os.makedirs(os.path.dirname(OUTPUT_FILE) or ".", exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        f.write(f"CNR:                {CNR}\n")
        f.write(f"Filing Number:      {filing_number or 'NOT FOUND'}\n")
        f.write(f"Registration Number: {registration_number or 'NOT FOUND'}\n")
    print(f"  📄 Results written to {OUTPUT_FILE}")

    if not filing_number and not registration_number:
        sys.exit(1)
