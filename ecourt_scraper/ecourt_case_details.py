#!/usr/bin/env python3
"""Fetch case details from eCourts.gov.in using a CNR number (CLI wrapper).

Usage:
    python ecourt_case_details.py KABC010220432024

Requires:
    pip install playwright requests
    playwright install chromium
"""

import sys

from .session import (
    EcourtSession,
    set_call_limit,
)

# ---------- CLI ----------

def main():
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <CNR_NUMBER>", file=sys.stderr)
        sys.exit(1)

    cnr = sys.argv[1].strip()
    if len(cnr) != 16:
        print("CNR number must be 16 characters", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching details for CNR: {cnr}\n", file=sys.stderr)

    set_call_limit(5)

    session = EcourtSession()
    try:
        details = session.search_case(cnr)

        print("=" * 60)
        print("  CASE DETAILS")
        print("=" * 60)
        print()

        print(f"  Registration Number:  {details['registration_number'] or 'N/A'}")

        print(f"\n  Petitioner(s) & Advocate(s):")
        for p in details["petitioner_advocate"]:
            line = f"    - {p['party']}"
            if p.get("advocate"):
                line += f"  (Adv: {p['advocate']})"
            print(line)

        print(f"\n  Respondent(s) & Advocate(s):")
        for r in details["respondent_advocate"]:
            line = f"    - {r['party']}"
            if r.get("advocate"):
                line += f"  (Adv: {r['advocate']})"
            print(line)

        print(f"\n  Case History ({len(details['case_history'])} rows):")
        if details["case_history"]:
            print(f"  {'Judge':<45} {'Business Date':<16} {'Hearing Date':<16} Purpose")
            print(f"  {'-'*44} {'-'*15} {'-'*15} {'-'*20}")
            for h in details["case_history"]:
                print(f"  {h['judge']:<45} {h['business_date']:<16} {h['hearing_date']:<16} {h['purpose']}")
        else:
            print("    (none found)")

        # ---- Business Details for latest 2 dates ----
        if session.ecourts_available:
            links = session.get_business_links()
            if links:
                print(f"\n{'='*60}")
                print(f"  BUSINESS OF THE DAY (latest 2 dates)")
                print(f"{'='*60}")

                for i, link in enumerate(links[:2]):
                    print(f"\n─── Date: {link['business_date']} ───")
                    biz = session.view_business(link)
                    print(f"    Business:       {biz.get('business', 'N/A')}")
                    print(f"    Next Purpose:   {biz.get('next_purpose', 'N/A')}")
                    print(f"    Next Hearing:   {biz.get('next_hearing_date', 'N/A')}")

                    if i == 0:
                        session.back_to_history()
        else:
            print("\n  (no clickable business links — family matter or unsupported case type)")

        print()

    finally:
        session.close()


if __name__ == "__main__":
    main()
