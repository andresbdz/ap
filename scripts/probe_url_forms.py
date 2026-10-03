"""Find a URL form that works for part numbers containing awkward characters.

1,866 of the 109,589 part numbers cannot be dropped into a URL path segment:
1,373 contain a slash, 529 a space, and a handful carry @, +, # or %. The
query form `?PartNumbers=` turns out to serve a generic page titled "Back
Button" rather than a part detail page, so another route is needed.

This tries each candidate once, at the normal pace, and reports which produced
a real part page. It stops at the first sign the portal is unhappy.

    .venv/bin/python scripts/probe_url_forms.py
"""

from __future__ import annotations

import sys
import time
from urllib.parse import quote

from lxml import html as LH

from txtav import extract, guard
from txtav.config import load_settings
from txtav.navigator import browser_session

# A part with a space, a part with a slash, and the plain part that the first
# one is a variant of, as a control.
CANDIDATES = ["000-110105-79", "000-110105-79 U", "000-110105-52U/P"]


def forms(pn: str, base: str) -> list[tuple[str, str]]:
    base = base.rstrip("/")
    detail = f"{base}/Parts/PartSearch/PartsDetail"
    return [
        ("path %20", f"{detail}/{quote(pn, safe='')}"),
        ("path raw", f"{detail}/{pn}"),
        ("query", f"{detail}?PartNumbers={quote(pn, safe='')}"),
    ]


def looks_like_a_part_page(html: str) -> tuple[bool, str]:
    doc = LH.fromstring(html)
    title = (doc.xpath("//title/text()") or ["?"])[0].strip()
    h1 = doc.xpath("//h1[contains(@class,'part-detail-title')]/text()")
    price = doc.xpath("//input[@id='YourPriceUS_0']/@value")
    ok = bool(h1)
    detail = f"title={title!r} h1={h1[0].strip() if h1 else None!r} price={price[0] if price else None!r}"
    return ok, detail


def main() -> int:
    settings = load_settings()
    with browser_session(settings.profile_dir, settings.page_timeout_seconds) as page:
        first = True
        for pn in CANDIDATES:
            print(f"\n=== {pn!r} ===")
            for name, url in forms(pn, settings.base_url):
                if not first:
                    time.sleep(settings.request_interval_seconds)
                first = False

                try:
                    response = page.goto(url, wait_until="domcontentloaded")
                except Exception as exc:  # noqa: BLE001
                    print(f"  {name:<10} ERROR {type(exc).__name__}: {exc}")
                    continue
                page.wait_for_load_state("networkidle")

                status = response.status if response else None
                text = page.inner_text("body")
                decision = guard.assess(status, page.url, extract.visible_text(page.content()))
                if decision.verdict is guard.Verdict.HALT:
                    print(f"  {name:<10} STOP: {decision.kind} {decision.detail}")
                    return 2
                if decision.verdict is guard.Verdict.REAUTH:
                    print("  session expired, run `txtav login`")
                    return 3

                ok, detail = looks_like_a_part_page(page.content())
                print(f"  {name:<10} HTTP {status} {'OK  ' if ok else 'no  '} {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
