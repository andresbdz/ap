# TXT Aviation parts availability workflow

Looks up the client's part numbers on the Textron Aviation customer portal
(`ww2.txtav.com`) and records price, stock and condition data against each one.

**Status: phase 1 of 7.** Offline foundations only. Nothing in this repository
contacts the portal yet.

## Scale and pacing

| | |
|---|---|
| Part numbers in scope (WH = TXT) | 109,589 |
| Cycle length | 14 days, continuous |
| Required pace | 7,828/day, 326/hour, 5.44/min |
| Interval between requests | 11.0 seconds |

There is no spare capacity in that schedule. An hour of downtime is roughly 300
parts that cannot be made up within the cycle.

`TXTAV_REQUEST_INTERVAL_SECONDS` controls the pace and is refused below 2.0
seconds, so a typo in `.env` cannot turn this into a hammer.

## Operating rules

These are design constraints, not preferences.

- The first login is performed by a human in a real browser window. The session
  persists in a Playwright profile directory and is reused.
- No stealth plugins, fingerprint spoofing, proxy rotation, CAPTCHA solving or
  cookie farming.
- Strictly sequential. One part at a time, never parallel.
- A 403, 429, CAPTCHA, forced logout or access warning raises a halt. The halt
  blocks every subsequent run until an operator clears it by hand. Nothing in
  the code may clear a halt automatically, and there is no retry after one.
- An empty or unparseable result is never written as a valid value. It becomes
  `manual_review` so a stale but correct price is not replaced by a blank.
- Credentials, cookies and one time codes are redacted from every log record by
  a processor, not by call sites.

Operational procedures are in [RUNBOOK.md](RUNBOOK.md).

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
cp .env.example .env
```

Put the client's workbook at `data/PN_List_full.xlsx`, or point
`TXTAV_PARTS_XLSX` elsewhere.

## Commands

```bash
.venv/bin/python -m txtav.cli import     # load the part list into the job store
.venv/bin/python -m txtav.cli status     # part counts, pacing, open halts
.venv/bin/python -m txtav.cli login      # sign in by hand, store the session
.venv/bin/python -m txtav.cli dryrun --limit 5   # fetch without writing
.venv/bin/python -m txtav.cli run                # process pending parts
.venv/bin/python -m txtav.cli results            # show captured rows
.venv/bin/python -m txtav.cli halt-clear --id N --by NAME
.venv/bin/python -m pytest -q            # 191 tests, no network
```

A halt blocks every run until a person clears it. That is deliberate: nothing
in the code may clear one.

```bash
.venv/bin/python scripts/capture_fixtures.py   # refresh the page fixtures
.venv/bin/python scripts/verify_sample.py      # field by field, for review
```

## Layout

| Module | Role |
|---|---|
| `config.py` | Settings from `.env`. Holds no credentials. |
| `db.py` | SQLite job store. The source of truth, not the spreadsheet. |
| `xlsx_reader.py` | Exact workbook reader. See the note below. |
| `importer.py` | Workbook to `parts` table, TXT filtered and deduplicated. |
| `urls.py` | Part detail URL construction and the search form fallback. |
| `extract.py` | One rendered page to one record, with cross checks. |
| `guard.py` | Stop conditions. Biased towards halting. |
| `pacer.py` | Request spacing and cycle planning. |
| `navigator.py` | The only module that drives the browser. |
| `runner.py` | The run loop: fetch, extract, commit, stop. |
| `rules.py` | Field logic from the scope document. Pure functions. |
| `logging_setup.py` | JSON logging with mandatory redaction. |
| `cli.py` | Entry point. |

## Two findings in the client's data

**2,930 part numbers are stored as numbers, not text.** 240 of those are held
in scientific notation and 26 additionally carry a date format. openpyxl hands
these back as floats and datetimes, turning part number `10003` into
`"10003.0"` and `100057022` into a date, which would have produced roughly
2,930 silent not-found results. `xlsx_reader.py` exists to read the stored
value verbatim instead. `tests/test_xlsx_reader.py` pins every observed case.

**1,373 part numbers contain a forward slash and need a different route.**
Measured against the live portal on 2026-10-02, not assumed:

| Form | Result |
|---|---|
| `/PartsDetail/<pn>` percent encoded | works, including for the 529 part numbers containing a space |
| `/PartsDetail/<pn>` with a slash, raw or as `%2F` | HTTP 404, ASP.NET refusing a double escaped path |
| `/PartsDetail?PartNumbers=<pn>` | never works; serves a generic page titled "Back Button" for any input. It is also the one pattern robots.txt disallows, so it is not used at all |
| `/Parts/PartSearch?partdesc=<pn>` | works for every case, and redirects to the canonical detail page |

Slash bearing part numbers therefore go through the site's own search form.
The portal writes the slash as a hyphen in the URL it redirects to, but we do
not perform that substitution ourselves: a part literally named
`000-110105-52U-P` could exist and we would silently read the wrong one.

A space is meaningful and must not be normalised away. `000-110105-79 U` is a
real part at $2,038.73, distinct from `000-110105-79` at $1,650.67.

**Discontinued parts show no price and a placeholder zero.** A part marked
"No longer available" keeps its description and usually offers a replacement
part number, but renders no price and no stock line while its hidden
`YourPriceUS_0` reads `0`. The hidden price is only trusted when a displayed
price corroborates it, so these rows carry a blank price rather than $0.00,
and the expected absences are not raised for review.

## Resolved questions

1. **Which price.** Confirmed by the client on 2026-10-02: the USD `Your Price`
   shown on the page, into column G. This account currently shows a 0%
   discount, which is expected. `list_price` is stored alongside anyway so a
   later change of mind is a re-export rather than a re-crawl.
2. **NEW / EXCHANGE / REPAIR SERVICES.** Not tabs on one part. Each button is a
   separate part number: `ChangePNandSubmit(0, '9912125-2')`, `'9912125-2EX'`,
   `'9912125-2REP'`. The page always opens on the part number that was
   requested, so there is nothing to choose. The active button is recorded in
   `tab_active` for auditability.

## Session lifetime

Being measured, because it decides whether a 24/7 schedule is realistic: every
re-login needs a human to read the 2FA code out of the shared mailbox.

| Signed in | Last working request | Found expired | Idle gap |
|---|---|---|---|
| 2026-10-02 08:38 | 2026-10-02 08:54 | 2026-10-02 19:17 | under 10h23m |

So the session does not survive a night unattended. Until this is pinned down
more precisely, assume a human has to sign in at least once a day.

## Open questions

- **Cadence.** The Drive copy of the scope says "for now we decided once a
  week"; the Slack copy says a 14 day cycle and derives the 5 to 6 per minute
  figure from it. Everything here is built to the 14 day cycle, which is the
  later and more detailed of the two. Worth confirming before the full run.
- **Multiple alternate part numbers.** `9912125-2EX` carries two: `9912125-2`
  labelled `NEW`, which is visible, and `9912125-2W` labelled
  `ALTERNATE PART NUMBER`, which is marked `display:none`. The scope says to
  take the field under the Create List button, so the visible one is used and
  the hidden one is recorded.

## Condition code

The client's rule, verbatim from the scope document: "Condition codes are only
ever NE or OH. And if the part number has the EX at the end, it's OH". The
suffix decides the value written to the sheet.

Note that the scope's "-EX" wording cannot be implemented literally: none of
the 1,997 list entries ending in `EX` carry a hyphen immediately before it
(`00033190-2EX`, `9912125-2EX`, `C662502-0101EX`). The implemented rule is a
trailing `EX`.

The page's own `Condition:` line is read as a cross check and a disagreement
flags the row for review. It does not change the written value.

## Phases

| | | Status |
|---|---|---|
| 1 | Store, importer, rules, URLs, logging, tests | done |
| 2 | Extractor against saved page fixtures | done |
| 3 | Session, halt guard, navigator, run loop | done |
| 4 | Verified sample, client confirms accuracy | awaiting client |
| 5 | Exporter, alerts, daily summary, runbook | done |
| 5b | Google Sheets writer | needs the sheet converted |
| 5c | Supervisor loop for continuous running | needs the host decided |
| 6 | Pilot of 500 parts over one day | client gate |
| 7 | Full cycle | only after phase 6 is approved |
