"""Print every extracted field for named parts, for human verification.

This is the phase 4 artefact: the client reads the table, opens the same part
numbers in their own browser, and confirms the values match before anything
runs at scale. Pages are saved so a disputed row can be re-checked later
without going back to the portal.

    .venv/bin/python scripts/verify_sample.py 6622168-22 9912125-2EX
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from txtav import extract, guard, logging_setup, urls
from txtav.config import load_settings
from txtav.navigator import Navigator, browser_session

DEFAULT_SAMPLE = [
    "6622168-22",        # plain part, dual currency price
    "260-1014-33",       # plain part, USD only
    "9912125-2EX",       # core deposit, alternate part, sibling part numbers
    "000-110105-79",     # plain part
    "000-110105-79 U",   # space in the part number, a distinct part
    "000-110105-52U/P",  # slash, reached through the search form
    "ZZZZ-9999",         # does not exist
]

FIELDS = (
    ("A  PART NUMBER", lambda r: r.pn_requested),
    ("B  Alternate part number", lambda r: r.alt_pn),
    ("C  Description", lambda r: r.description),
    ("D  Condition code", lambda r: r.condition_code if r.found else None),
    ("E  Quantity", lambda r: r.quantity),
    ("F  Outright price", lambda r: r.outright_price),
    ("G  Sale price", lambda r: r.sale_price),
    ("H  Core value", lambda r: r.core_value if r.found else None),
    ("I  Price reference", lambda r: "TXT" if r.found else None),
    ("   Type (no column yet)", lambda r: r.part_type if r.found else None),
)


def main(argv: list[str]) -> int:
    logging_setup.configure(None, "WARNING")
    settings = load_settings()
    sample = argv or DEFAULT_SAMPLE
    out_dir = Path(settings.fixtures_dir) / "verify"

    with browser_session(settings.profile_dir, settings.page_timeout_seconds) as page:
        nav = Navigator(page, settings.base_url)
        if not nav.is_signed_in():
            print("Not signed in. Run `txtav login` first.")
            return 3

        for index, pn in enumerate(sample):
            if index:
                time.sleep(settings.request_interval_seconds)

            print(f"\n{'=' * 72}\n{pn}\n  via {urls.detail_url(pn, settings.base_url)}")
            fetched = nav.fetch(pn)
            print(f"  HTTP {fetched.status} -> {fetched.final_url}")

            out_dir.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in pn)
            (out_dir / f"{safe}.html").write_text(fetched.html, encoding="utf-8")

            if fetched.decision.verdict is guard.Verdict.HALT:
                print(f"  STOPPED: {fetched.decision.kind} {fetched.decision.detail}")
                return 2
            if fetched.decision.verdict is guard.Verdict.REAUTH:
                print("  session expired, run `txtav login`")
                return 3

            try:
                record = extract.extract(fetched.html, pn)
            except extract.ExtractionError as exc:
                print(f"  NOT A PART PAGE: {exc}")
                continue

            if not record.found:
                print("  PART NOT FOUND on the portal")
                continue

            for label, getter in FIELDS:
                value = getter(record)
                print(f"  {label:<28} {'' if value is None else value}")

            print(f"  {'   page says Condition':<28} {record.page_condition}")
            print(f"  {'   page says List Price':<28} {record.list_price}")
            print(f"  {'   displayed Your Price':<28} {record.sale_price_rendered}")
            if record.alt_hidden:
                print(f"  {'   hidden alternates':<28} {record.alt_hidden}")
            if record.tabs:
                print(f"  {'   sibling part numbers':<28} {record.tabs}")
                print(f"  {'   active':<28} {record.tab_active}")
            if record.conflicts:
                for conflict in record.conflicts:
                    print(f"  NEEDS REVIEW: {conflict}")

    print(f"\nPages saved under {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
