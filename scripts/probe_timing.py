"""Test whether the portal's HTTP 500s are caused by the gap between requests.

On the first real run, three of five parts returned 500 on the first attempt
and succeeded on retry a few seconds later. Lining the log up by time showed
the 500s landing on requests that followed an idle gap of about 11 seconds,
and the successes on requests that followed within a few seconds. That is a
hypothesis, not a finding, so this measures it.

The same part is requested repeatedly with a controlled gap before each one.
If the hypothesis holds, the long gaps fail and the short gaps do not.

Deliberately small: 10 requests, no part looked up that the run would not have
looked up anyway, and it stops at the first sign the portal is unhappy.

    .venv/bin/python scripts/probe_timing.py
"""

from __future__ import annotations

import sys
import time

from txtav import extract, guard, urls
from txtav.config import load_settings
from txtav.navigator import browser_session

PART = "000-110105-72"

# Alternating so a drift over time cannot be mistaken for the effect.
GAPS = [0, 11, 2, 11, 30, 2, 11, 5, 20, 3]


def main() -> int:
    settings = load_settings()
    url = urls.detail_url(PART, settings.base_url)
    results: list[tuple[int, int | None]] = []

    with browser_session(settings.profile_dir, settings.page_timeout_seconds) as page:
        page.set_default_timeout(settings.page_timeout_seconds * 1000)
        print(f"{PART}\n{url}\n")
        print(f"{'gap':>5}  {'status':>6}  note")

        for gap in GAPS:
            if gap:
                time.sleep(gap)
            try:
                response = page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                print(f"{gap:>5}  {'ERR':>6}  {type(exc).__name__}: {exc}")
                results.append((gap, None))
                continue

            status = response.status if response else None
            results.append((gap, status))

            text = extract.visible_text(page.content())
            decision = guard.assess(status, page.url, text)
            if decision.verdict is guard.Verdict.HALT:
                print(f"{gap:>5}  {status:>6}  STOP: {decision.kind}")
                return 2
            if decision.verdict is guard.Verdict.REAUTH:
                print(f"{gap:>5}  {status:>6}  session expired, run `txtav login`")
                return 3
            print(f"{gap:>5}  {status:>6}  {'ok' if status == 200 else 'server error'}")

    short = [s for gap, s in results if gap <= 5]
    long = [s for gap, s in results if gap >= 11]
    print("\nsummary")
    print(f"  gap <= 5s : {sum(1 for s in short if s == 200)}/{len(short)} succeeded")
    print(f"  gap >= 11s: {sum(1 for s in long if s == 200)}/{len(long)} succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
