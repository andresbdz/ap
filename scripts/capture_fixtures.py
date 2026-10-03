"""Save rendered part detail pages so the extractor can be built offline.

Run once with a logged in profile. Visits a few parts chosen to cover the
shapes the extractor must handle and writes each rendered page into the
fixtures directory. Phase 2 then needs no portal traffic at all.

    .venv/bin/python scripts/capture_fixtures.py
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from txtav import urls
from txtav.config import load_settings

# Chosen for coverage, not at random:
#   6622168-22    no condition line, no core deposit, dual currency price
#   260-1014-33   no condition line, USD only, NOTES block, no alternate part
#   9912125-2EX   three tabs, core deposit, alternate part number panel
#   ZZZZ-9999     does not exist, so we learn what a not-found page looks like
SAMPLES = ["6622168-22", "260-1014-33", "9912125-2EX", "ZZZZ-9999"]

# Text meaning the portal is unhappy with us. Any of these aborts the capture.
STOP_SIGNALS = (
    "request access to view pricing",
    "access denied",
    "too many requests",
    "unusual activity",
    "are you a robot",
    "verify you are human",
)

LOGGED_OUT_SIGNALS = ("sign in", "log in", "/account/login")


def safe_name(pn: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", pn)


def main() -> int:
    settings = load_settings()
    out_dir = Path(settings.fixtures_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(settings.profile_dir), headless=False, viewport=None
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(settings.page_timeout_seconds * 1000)

        try:
            for index, pn in enumerate(SAMPLES):
                if index:
                    time.sleep(settings.request_interval_seconds)

                url = urls.detail_url(pn, settings.base_url)
                print(f"\n[{index + 1}/{len(SAMPLES)}] {pn}\n  {url}")

                response = page.goto(url, wait_until="domcontentloaded")
                status = response.status if response else None
                page.wait_for_load_state("networkidle")

                body = page.content()
                lowered = body.lower()

                if status in (401, 403, 429):
                    print(f"  STOP: portal returned HTTP {status}")
                    return 2

                hit = next((s for s in STOP_SIGNALS if s in lowered), None)
                if hit is not None:
                    print(f"  STOP: page contains {hit!r}")
                    return 2

                if any(s in page.url.lower() for s in LOGGED_OUT_SIGNALS):
                    print(f"  STOP: redirected to {page.url}, session not valid")
                    return 3

                target = out_dir / f"{safe_name(pn)}.html"
                target.write_text(body, encoding="utf-8")
                print(
                    f"  HTTP {status} | title {page.title()!r}"
                    f"\n  saved {target} ({len(body):,} bytes)"
                )
        finally:
            context.close()

    print(f"\nDone. {len(SAMPLES)} pages in {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
