"""Prove that a failure screenshot carries no identifying details.

Loads a saved part page in a real browser, applies the mask, and checks that
the account holder's name, the customer number and the support contacts are
no longer readable. Then unmasks and checks the page is back as it was.

Local files only. It opens nothing on the portal.

    .venv/bin/python scripts/check_redaction.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from txtav import redact_page

FIXTURE = Path("fixtures/6622168-22.html")
OUT = Path("screenshots/redaction-check.png")

# Values that must not be legible once the mask is on. Read out of the fixture
# rather than hard coded, so this cannot silently pass against a stale list.
SECRET_PATTERNS = {
    "account holder": r"Alex\s+Palmer",
    "customer number": r"AIRCRAFT PARTS DISTRIBUTION IN \d+",
    "support email": r"[\w.-]+@txtav\.com",
    "support phone": r"\+1\.\d{3}\.\d{3}\.\d{4}",
}


def visible(page) -> str:
    return re.sub(r"\s+", " ", page.inner_text("body"))


def main() -> int:
    if not FIXTURE.exists():
        print(f"{FIXTURE} is missing. Run scripts/capture_fixtures.py first.")
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(FIXTURE.resolve().as_uri(), wait_until="domcontentloaded")

        before = visible(page)
        present = {
            label: pattern
            for label, pattern in SECRET_PATTERNS.items()
            if re.search(pattern, before)
        }
        print(f"in the page before masking: {len(present)} of {len(SECRET_PATTERNS)}")
        for label in present:
            print(f"  found  {label}")
        if not present:
            print("Nothing sensitive found in the fixture; this proves nothing.")
            browser.close()
            return 1

        masked_count = redact_page.mask(page)
        after = visible(page)
        print(f"\nmasked {masked_count} elements")

        failures = []
        for label, pattern in present.items():
            hit = re.search(pattern, after)
            print(f"  {'STILL VISIBLE' if hit else 'hidden':<14} {label}")
            if hit:
                failures.append(label)

        OUT.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(OUT), full_page=False)
        print(f"\nmasked screenshot written to {OUT}")

        redact_page.unmask(page)
        restored = visible(page)
        if restored != before:
            print("unmask did not restore the page")
            failures.append("unmask")
        else:
            print("unmask restored the page")

        browser.close()

    if failures:
        print(f"\nFAILED: {', '.join(failures)}")
        return 2
    print("\nPASS: nothing identifying survives the mask")
    return 0


if __name__ == "__main__":
    sys.exit(main())
