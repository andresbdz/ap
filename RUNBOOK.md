# Runbook

What to do when something happens. Written for whoever is on duty, not for
whoever wrote the code.

## The one rule

**When the tool stops, it stopped for a reason. Find the reason before
restarting it.**

A halt means the portal told us something we should listen to. Clearing one
without reading it is how an account gets blocked, and this account is how the
business buys parts.

---

## Daily

```bash
.venv/bin/python -m txtav.cli summary
```

Read three lines:

| Line | Healthy | Act if |
|---|---|---|
| `progress` | climbing by roughly 7,800 a day | flat since yesterday |
| `schedule` | `on track` | `BEHIND by N parts` |
| `STOPPED` | absent | present, see below |

If the session has lapsed, sign in again:

```bash
.venv/bin/python -m txtav.cli login
```

A browser opens. Sign in by hand, including the 2FA code from the shared
mailbox, then press Enter in the terminal. It prints `Signed in` or
`NOT signed in`. Nothing else is automated here, deliberately.

---

## When it has stopped

`summary` and `run` both print the halt. There are four kinds and they are not
equally serious.

### `session_expired`

The login lapsed. Ordinary, not a warning from the portal.

```bash
.venv/bin/python -m txtav.cli login            # sign in by hand
.venv/bin/python -m txtav.cli halt-clear --id N --by "your name"
.venv/bin/python -m txtav.cli run
```

### `rate_limited`, `forbidden`, `unauthorized`

HTTP 429, 403 or 401. **The portal is refusing us.** Do not clear and retry.

1. Open the portal in an ordinary browser and sign in as a person. Does it work?
2. Look at `screenshots/` for the image captured at the moment it stopped.
   Identifying details are masked; the error is not.
3. Tell the account owner before doing anything else.
4. Only clear it once someone has decided it is safe, and consider raising
   `TXTAV_REQUEST_INTERVAL_SECONDS` before the next run.

### `captcha`, `access_warning`, `account_locked`

**Stop. Do not clear this.** The portal has said, in words, that it is unhappy
with the traffic. Escalate to the account owner. Restarting risks the account.

### `pricing_revoked`

The page offered "Request Access to View Pricing" where a price belongs. The
account has lost pricing access, which it has done before. No amount of
retrying fixes it. Contact the account owner; this is a portal permissions
question, not a software one.

### `consecutive_failures`

Eight parts in a row failed with nothing else to explain it, which usually
means quiet blocking rather than eight unrelated faults. Each of those eight was
already three exhausted attempts, so this is roughly twenty four failed requests.
Treat it as the `rate_limited` case.

### `unrecognised_page`

A page loaded, the guard was happy with it, and it was either not a part page or
carried a field we could not read, such as a price heading with words where an
amount belongs. Usually the portal's markup or wording changed. Look at the saved
HTML, fix the selectors in `txtav/extract.py`, run the tests, then clear.

### `extractor_failed`

The extractor raised something nobody anticipated. This is a bug in our code
rather than a state of the portal. The detail names the exception. Fix it, add a
test for the page that caused it, then clear.

### `browser_gone`

Chromium died: the machine slept, ran out of memory, or was killed. Nothing
suggests the portal blocked us. Check the machine stayed up, then clear this and
start again. Nothing is lost; the part that was in flight is picked up again.

---

## Falling behind

```
schedule      BEHIND by 4,210 parts, cycle ends 2026-10-16 12:19:11
```

The cycle has a fixed deadline and no spare capacity: 109,589 parts over 14
days is one every 11.0 seconds, continuously. An hour lost is about 327 parts
that cannot be made up inside the window.

Options, in order of preference:

1. **Accept a longer cycle.** Say so and let it run. The data is still correct.
2. **Find the lost time.** Did the machine sleep? Did the session lapse
   overnight? Fix the cause rather than the symptom.
3. **Shorten the interval.** Last resort, and never without the account
   owner's agreement, because it means more load on the portal.

Never edit the database to mark parts done.

---

## Routine commands

```bash
.venv/bin/python -m txtav.cli import              # reload the part list
.venv/bin/python -m txtav.cli status              # counts, pacing, open halts
.venv/bin/python -m txtav.cli login               # sign in by hand
.venv/bin/python -m txtav.cli dryrun --limit 5    # fetch without writing
.venv/bin/python -m txtav.cli run                 # process pending parts
.venv/bin/python -m txtav.cli run --limit 50      # a bounded run
.venv/bin/python -m txtav.cli results --limit 20  # see captured rows
.venv/bin/python -m txtav.cli export --out data/Output.xlsx
.venv/bin/python -m txtav.cli summary --send      # also post to the webhook
.venv/bin/python -m txtav.cli halt-clear --id N --by NAME
```

Verification, when something looks wrong:

```bash
.venv/bin/python -m pytest -q                     # 246 tests, no network
.venv/bin/python scripts/verify_sample.py         # field by field, live
.venv/bin/python scripts/check_redaction.py       # prove screenshots are safe
```

---

## Keeping it running

```bash
.venv/bin/python -m txtav.cli supervise
```

That keeps the cycle moving: it runs passes, waits when the queue is empty or
the cycle is not due yet, and stops the moment anything says it should. Ctrl-C
stops it cleanly at the next pass boundary. It never retries past a halt; it is
a scheduler, not a way around the guard.

It stops by itself, with an alert, for two reasons you have to act on:

- **session expired** — run `txtav login`, sign in by hand, start it again
- **halted** — read the halt first, see above

Every 8 passes, roughly five hours, it closes the browser and opens a new one.
That is deliberate: one Chromium tab is not built to navigate 109,589 pages
without being replaced, and the session lives in `.pw-profile/` rather than in
the browser, so reopening costs a couple of seconds and loses nothing. Expect
`browser_recycled` in the log. `--session-passes` changes the interval.

On a machine that sleeps, wrap it:

```bash
caffeinate -dimsu .venv/bin/python -m txtav.cli supervise
```

Whatever machine is used, **the login must happen on that same machine**: the
session belongs to the browser profile in `.pw-profile/`, and moving it changes
both the IP and the browser environment mid-session.

### Moving to another machine

Copy across, by remote desktop file transfer and never by email or chat:

| Copy | Why |
|---|---|
| `data/txtav.db` | every result so far, and the place in the cycle |
| `.env` | settings, including the sheet id |
| `secrets/` | the Google token; without it results never reach the sheet |

**Do not copy `.pw-profile/`.** It is tied to one Chromium build, and the
portal session is signed in again by hand on the new machine regardless.

Then, from the project directory on the new machine:

```bash
.venv/bin/python -m txtav.cli preflight            # fix every FAIL
.venv/bin/python -m txtav.cli cycle-restart        # if preflight says the cycle is behind
.venv/bin/python -m txtav.cli login                # by hand, with the 2FA code
.venv/bin/python -m txtav.cli supervise
```

The machine must not sleep, must stay plugged in, and must not reboot itself
for updates during a cycle. Remote access has to be unattended, or every
expired session waits for someone at that keyboard to accept a connection.

---

## Things never to do

- Never clear a halt you have not read.
- Never lower `TXTAV_REQUEST_INTERVAL_SECONDS` to catch up. It is refused below
  2.0 seconds and should not be touched without the account owner's agreement.
- Never run two copies at once. One browser, one tab, strictly sequential.
- Never commit `.pw-profile/`, `data/`, `fixtures/`, `logs/` or `.env`. They
  hold live session cookies, the client's part list, and pages carrying the
  account holder's name.
- Never edit the Output sheet by hand while a run is in progress. The sheet is
  overwritten row by row from the database.
- Never automate the login or the 2FA code. The manual step is the point.

---

## Where things are

| | |
|---|---|
| Job store, the source of truth | `data/txtav.db` |
| Browser session | `.pw-profile/` |
| Logs, JSON, rotated at 20 MB, 10 kept | `logs/txtav.log` |
| Screenshots of stoppages, masked | `screenshots/` |
| Settings | `.env`, documented in `.env.example` |
| Google credentials and token | `secrets/`, gitignored |
| Output spreadsheet | the Google Sheet named by `TXTAV_SHEET_ID` |

The spreadsheet is a projection of the database, never the other way round. If
the two disagree, the database is right:

```bash
.venv/bin/python -m txtav.cli sheet-push
```

That rewrites all 109,589 rows in about 50 seconds. The sheet also updates
during a run, so a push is only needed after a failed live update or if someone
has edited the sheet by hand. A run that could not write some rows says so at
the end and raises a `sheet_behind` alert naming them, so this is never
something to be guessed at.

If the header row itself has been damaged, add `--repair-header` to rebuild row
1 as well.

Do not reach for `--processed-only` here; it is refused. Writing only the parts
done so far would shift every row and shrink the tab, which is how 1,148 rows
came to carry another part's prices on 2026-10-06. A full push is safe at any
point in a cycle: parts not yet reached simply keep the value they already had.
For a sample to look at or send, write a file instead:

```bash
.venv/bin/python -m txtav.cli export --processed-only --out data/sample.xlsx
```

A row with a blank price is not necessarily a fault. `summary` counts the three
reasons separately: discontinued, sold on quotation, and listed but not
offered.
