"""The run loop.

Takes pending parts from the job store one at a time, fetches, extracts and
commits each one, and stops the moment anything says it should.

Commit granularity is one part. A kill at any moment loses at most the part in
flight, which is then picked up again on resume because it is left in
`in_flight` and reset at startup.
"""

from __future__ import annotations

import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from txtav import alerts, db, extract, guard, live_sheet, logging_setup, redact_page, rules
from txtav.config import Settings
from txtav.navigator import Navigator
from txtav.pacer import Pacer

log = logging_setup.get_logger("txtav.runner")


@dataclass
class RunReport:
    attempted: int = 0
    success: int = 0
    not_found: int = 0
    failed: int = 0
    manual_review: int = 0
    requests: int = 0
    server_errors: int = 0
    halted: guard.Decision | None = None
    # The pass ended early because the browser stopped answering. Not a halt;
    # the supervisor replaces the browser and continues.
    browser_suspect: bool = False
    # The pass ended early because this machine lost its network.
    offline: bool = False
    started_at: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def summary(self) -> str:
        rate = self.attempted / (self.elapsed / 60) if self.elapsed > 0 else 0
        lines = [
            f"attempted:     {self.attempted}",
            f"  success:     {self.success}",
            f"  not found:   {self.not_found}",
            f"  failed:      {self.failed}",
            f"  for review:  {self.manual_review}",
            f"requests sent: {self.requests}"
            + (
                f"  ({self.server_errors} were HTTP 5xx, "
                f"{100 * self.server_errors / self.requests:.0f}%)"
                if self.requests
                else ""
            ),
            f"elapsed:       {self.elapsed / 60:.1f} min ({rate:.2f} parts/min)",
        ]
        if self.halted is not None:
            lines.append(f"HALTED: {self.halted.kind} - {self.halted.detail}")
        return "\n".join(lines)


@dataclass
class CycleProgress:
    """Where a cycle stands against its own deadline."""

    cycle_id: int
    total: int
    done: int
    elapsed_fraction: float
    ends_at: str
    total_days: float = 14.0

    @property
    def expected_done(self) -> int:
        """How many parts should be finished by now to land on the deadline."""
        return int(self.total * min(1.0, max(0.0, self.elapsed_fraction)))

    @property
    def behind_by(self) -> int:
        return max(0, self.expected_done - self.done)

    @property
    def on_schedule(self) -> bool:
        # One hour of slack, measured from this cycle's own length rather than
        # from a hard coded 14 days: a shorter cycle has to run faster, so an
        # hour of it is fewer parts, and a fixed divisor would quietly call a
        # late run healthy.
        if self.total_days <= 0:
            return True
        return self.behind_by <= (self.total / (self.total_days * 24))

    def describe(self) -> str:
        return (
            f"cycle {self.cycle_id}: {self.done:,} of {self.total:,} done, "
            f"{self.expected_done:,} expected by now, ends {self.ends_at}"
            + ("" if self.on_schedule else f", behind by {self.behind_by:,}")
        )


# Consecutive parts that time out with no answer at all before the pass ends so
# the browser can be replaced. Two, because one is an ordinary slow page and a
# tripwire halt at eight would otherwise be the first response to a hung tab.
BROWSER_SUSPECT_AFTER = 2


def build_pacer(settings: Settings) -> Pacer:
    """The pacer as configured. One place, so the runner and supervisor agree."""
    return Pacer(
        settings.request_interval_seconds,
        settings.jitter_seconds,
        slowdown_threshold=settings.slowdown_threshold,
        max_interval=settings.max_interval_seconds,
    )


def start_cycle(conn: sqlite3.Connection, cycle_days: int) -> int:
    """Open a cycle and queue every part in it."""
    with db.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO cycles(ends_at) VALUES (datetime('now', ?))",
            (f"+{cycle_days} days",),
        )
        cycle_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO jobs(cycle_id, pn) SELECT ?, pn FROM parts", (cycle_id,)
        )
    log.info("cycle_started", cycle_id=cycle_id, cycle_days=cycle_days)
    return cycle_id


def restart_cycle_window(
    conn: sqlite3.Connection, cycle_id: int, cycle_days: int
) -> tuple[str, str]:
    """Start a running cycle's window now, keeping every result it already has.

    For when the cycle was opened long before the run that will actually carry
    it: cycle 1 was opened by the pilot on 2026-10-02, and moved as is to the
    machine that runs it for real it would begin days late, report itself
    tens of thousands of parts behind on every pass, and could not finish by its
    own end date however well it ran. Nothing already looked up is redone; only
    the dates move. Returns the new (started_at, ends_at).
    """
    with db.transaction(conn):
        cursor = conn.execute(
            "UPDATE cycles SET started_at = datetime('now'),"
            " ends_at = datetime('now', ?) WHERE id = ? AND status = 'running'",
            (f"+{cycle_days} days", cycle_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"cycle {cycle_id} is not running")
        row = conn.execute(
            "SELECT started_at, ends_at FROM cycles WHERE id = ?", (cycle_id,)
        ).fetchone()
        db.log_event(
            conn,
            "warning",
            "cycle_window_restarted",
            f"cycle {cycle_id} now runs {row['started_at']} to {row['ends_at']}",
        )
    return row["started_at"], row["ends_at"]


def finish_cycle_if_complete(conn: sqlite3.Connection, cycle_id: int) -> bool:
    """Close a cycle once nothing is left to look up. Returns True if closed."""
    remaining = conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE cycle_id = ?"
        " AND status IN ('pending', 'in_flight')",
        (cycle_id,),
    ).fetchone()["n"]
    if remaining:
        return False

    conn.execute(
        "UPDATE cycles SET status = 'finished', finished_at = datetime('now')"
        " WHERE id = ? AND status = 'running'",
        (cycle_id,),
    )
    db.log_event(conn, "info", "cycle_finished", f"cycle {cycle_id} completed")
    log.info("cycle_finished", cycle_id=cycle_id)
    return True


def current_cycle(conn: sqlite3.Connection, cycle_days: int) -> int | None:
    """The cycle to work on, or None when the next one is not due yet.

    The scope asks for the same list to be searched again every 14 days. A
    cycle is therefore a window, not a queue: finishing early means waiting
    until the window closes rather than starting over immediately and doubling
    the load on the portal.

    Returns None while waiting. Before this existed nothing ever marked a cycle
    finished, so the first one stayed open forever and a second never began:
    the list would have been searched once and then the system would have sat
    idle indefinitely.
    """
    running = conn.execute(
        "SELECT id FROM cycles WHERE status = 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if running is not None:
        cycle_id = int(running["id"])
        if not finish_cycle_if_complete(conn, cycle_id):
            return cycle_id

    last = conn.execute(
        "SELECT id, ends_at, datetime('now') >= ends_at AS due FROM cycles"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last is not None and not last["due"]:
        log.info("cycle_not_due", previous=int(last["id"]), next_due=last["ends_at"])
        return None

    return start_cycle(conn, cycle_days)


def cycle_progress(conn: sqlite3.Connection, cycle_id: int) -> CycleProgress | None:
    """Measure a cycle against its deadline, so falling behind is visible.

    Without this a run that loses six hours to a sleeping machine or an expired
    session simply carries on at its normal pace and nothing says the deadline
    has already been missed.
    """
    row = conn.execute(
        "SELECT started_at, ends_at,"
        " (julianday('now') - julianday(started_at)) AS elapsed_days,"
        " (julianday(ends_at) - julianday(started_at)) AS total_days"
        " FROM cycles WHERE id = ?",
        (cycle_id,),
    ).fetchone()
    if row is None or not row["total_days"]:
        return None

    counts = conn.execute(
        "SELECT COUNT(*) total,"
        " SUM(status NOT IN ('pending', 'in_flight')) done"
        " FROM jobs WHERE cycle_id = ?",
        (cycle_id,),
    ).fetchone()

    return CycleProgress(
        cycle_id=cycle_id,
        total=int(counts["total"] or 0),
        done=int(counts["done"] or 0),
        elapsed_fraction=float(row["elapsed_days"]) / float(row["total_days"]),
        ends_at=row["ends_at"],
        total_days=float(row["total_days"]),
    )


def reset_stale(conn: sqlite3.Connection, cycle_id: int) -> int:
    """Return parts abandoned mid flight by a previous run to the queue."""
    cursor = conn.execute(
        "UPDATE jobs SET status = 'pending' WHERE cycle_id = ? AND status = 'in_flight'",
        (cycle_id,),
    )
    if cursor.rowcount:
        log.warning("reset_stale_jobs", cycle_id=cycle_id, count=cursor.rowcount)
    return cursor.rowcount


def pending(
    conn: sqlite3.Connection, cycle_id: int, limit: int | None
) -> list[tuple[str, int]]:
    """Pending parts as (part number, output row), in the output tab's order.

    The output row travels with the part because it is the sheet row that part
    occupies, which is what makes a live update one contiguous write. It is not
    the source row: the input tab also holds 200 MHI and BBD rows, so the two
    differ by an offset that nothing should be relying on.
    """
    sql = (
        "SELECT j.pn, p.output_row FROM jobs j JOIN parts p ON p.pn = j.pn "
        "WHERE j.cycle_id = ? AND j.status = 'pending' ORDER BY p.output_row"
    )
    params: list[object] = [cycle_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [(row["pn"], int(row["output_row"])) for row in conn.execute(sql, params)]


# The values whose change makes a row "updated". Everything a reader of the
# sheet acts on, and nothing that only describes how it was read.
TRACKED = (
    "description",
    "availability",
    "quantity",
    "sale_price",
    "list_price",
    "condition_code",
    "core_value",
    "outright_price",
    "part_type",
    "alt_pn",
)


def _store(conn: sqlite3.Connection, cycle_id: int, record: extract.PartRecord) -> None:
    def money(value) -> str | None:
        return None if value is None else f"{value:.2f}"

    values = {
        "description": record.description,
        "availability": record.availability,
        "quantity": record.quantity,
        "sale_price": money(record.sale_price),
        "list_price": money(record.list_price),
        "condition_code": record.condition_code,
        "core_value": money(record.core_value),
        "outright_price": money(record.outright_price),
        "part_type": record.part_type,
        "alt_pn": record.alt_pn,
    }

    # Last Updated carries forward while nothing changes. Compared against the
    # newest row for this part in any cycle, including this one, so a part read
    # twice in a cycle after a requeue does not count as changed.
    previous = conn.execute(
        f"SELECT {', '.join(TRACKED)}, updated_at, scraped_at FROM results"
        " WHERE pn = ? ORDER BY cycle_id DESC, scraped_at DESC LIMIT 1",
        (record.pn_requested,),
    ).fetchone()
    unchanged = previous is not None and all(
        previous[name] == values[name] for name in TRACKED
    )
    updated_at = (previous["updated_at"] or previous["scraped_at"]) if unchanged else None

    conn.execute(
        "INSERT OR REPLACE INTO results(cycle_id, pn, description, availability, quantity,"
        " sale_price, list_price, condition_code, core_value, outright_price,"
        " part_type, alt_pn, tab_active, conflict, conflict_note, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, COALESCE(?, datetime('now')))",
        (
            cycle_id,
            record.pn_requested,
            values["description"],
            values["availability"],
            values["quantity"],
            values["sale_price"],
            values["list_price"],
            values["condition_code"],
            values["core_value"],
            values["outright_price"],
            values["part_type"],
            values["alt_pn"],
            record.tab_active,
            int(bool(record.conflicts)),
            "; ".join(record.conflicts) or None,
            updated_at,
        ),
    )


def _finish(conn: sqlite3.Connection, cycle_id: int, pn: str, status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, finished_at = datetime('now'), error = ?"
        " WHERE cycle_id = ? AND pn = ?",
        (status, error, cycle_id, pn),
    )


def run(
    conn: sqlite3.Connection,
    navigator: Navigator,
    settings: Settings,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    save_pages: Path | None = None,
    pace: Pacer | None = None,
    tripwire: guard.ConsecutiveFailureTripwire | None = None,
) -> RunReport:
    """Process pending parts until the queue empties, the limit is hit, or we stop.

    The pacer and the tripwire are accepted rather than always built here so a
    supervisor can keep one of each across its passes. Built fresh per pass they
    both forget what came before: the pacer let the first request of a pass go
    out without waiting, and the failure count reset every 200 parts, so a slow
    drip of failures either side of a boundary never tripped anything.
    """
    report = RunReport()

    halt = db.open_halt(conn)
    if halt is not None:
        report.halted = guard.Decision(
            guard.Verdict.HALT, halt["kind"], f"halt #{halt['id']} is still open"
        )
        return report

    cycle_id = current_cycle(conn, settings.cycle_days)
    if cycle_id is None:
        log.info("waiting_for_next_cycle")
        return report

    reset_stale(conn, cycle_id)
    queue = pending(conn, cycle_id, limit)
    if not queue:
        finish_cycle_if_complete(conn, cycle_id)
        log.info("nothing_pending", cycle_id=cycle_id)
        return report

    progress = cycle_progress(conn, cycle_id)
    if progress is not None and not progress.on_schedule:
        log.warning("behind_schedule", detail=progress.describe())
        db.log_event(conn, "warning", "behind_schedule", progress.describe())

    live = None if dry_run else live_sheet.build(settings)
    if live is None and settings.sheet_id and not dry_run:
        _report_sheet_unavailable(conn, settings)
    if pace is None:
        pace = build_pacer(settings)
    if tripwire is None:
        tripwire = guard.ConsecutiveFailureTripwire(settings.consecutive_failure_halt)

    log.info(
        "run_started",
        cycle_id=cycle_id,
        queued=len(queue),
        interval=settings.request_interval_seconds,
        dry_run=dry_run,
    )

    def outcome(status: str) -> str:
        # A dry run reads but must not consume. Marking parts done without
        # storing anything made the real run skip them for the rest of the
        # cycle: every `txtav dryrun` quietly lost its parts' prices.
        return db.STATUS_PENDING if dry_run else status

    hung: list[str] = []  # consecutive parts that timed out without any answer

    for pn, output_row in queue:
        conn.execute(
            "UPDATE jobs SET status = 'in_flight', attempts = attempts + 1,"
            " started_at = datetime('now') WHERE cycle_id = ? AND pn = ?",
            (cycle_id, pn),
        )
        report.attempted += 1

        attempt = _attempt(navigator, settings, pn, pace, report)
        decision, record, error = attempt.decision, attempt.record, attempt.error
        if save_pages is not None and attempt.html:
            _save_page(save_pages, pn, attempt.html)

        if decision is not None and decision.should_stop:
            save_failure_screenshot(navigator, settings, pn, decision.kind)
            _finish(conn, cycle_id, pn, db.STATUS_PENDING, decision.detail)
            db.raise_halt(conn, decision.kind, decision.detail)
            report.halted = decision
            log.critical("halted", kind=decision.kind, detail=decision.detail, pn=pn)
            break

        if attempt.offline:
            # Back in the queue and out of the pass. Not a failure, so the
            # tripwire does not count it; the supervisor waits for the network
            # with the browser closed and carries on when it returns.
            _finish(conn, cycle_id, pn, db.STATUS_PENDING, "machine was offline")
            report.offline = True
            break

        if record is None:
            _finish(conn, cycle_id, pn, outcome(db.STATUS_FAILED), error)
            report.failed += 1
            log.warning("part_failed", pn=pn, error=error, timed_out=attempt.timed_out)
            trip = tripwire.record_failure()
            if trip.should_stop:
                db.raise_halt(conn, trip.kind, trip.detail)
                report.halted = trip
                log.critical("halted", kind=trip.kind, detail=trip.detail)
                break
            hung = hung + [pn] if attempt.timed_out else []
            if len(hung) >= BROWSER_SUSPECT_AFTER:
                # Not a halt: nothing here says the portal refused us, only
                # that the page never answered. The supervisor replaces the
                # browser and carries on; the tripwire keeps counting across
                # that, so a real outage still ends in a halt. The parts go
                # back in the queue, because failed is final for the cycle and
                # the fault was the browser's, not theirs.
                for stuck in hung:
                    _finish(conn, cycle_id, stuck, db.STATUS_PENDING, "browser stopped answering")
                report.browser_suspect = True
                log.warning("browser_suspect", pn=pn, requeued=hung)
                break
            continue

        hung = []
        tripwire.record_success()
        # Result and job status in one transaction, so a kill between them
        # cannot leave a stored result whose job still reads in_flight.
        with db.transaction(conn):
            if not dry_run:
                _store(conn, cycle_id, record)
            if not record.found:
                _finish(conn, cycle_id, pn, outcome(db.STATUS_NOT_FOUND))
                report.not_found += 1
            elif record.conflicts:
                _finish(
                    conn,
                    cycle_id,
                    pn,
                    outcome(db.STATUS_MANUAL_REVIEW),
                    "; ".join(record.conflicts),
                )
                report.manual_review += 1
            else:
                _finish(conn, cycle_id, pn, outcome(db.STATUS_SUCCESS))
                report.success += 1

        if record.found and record.conflicts:
            log.warning("needs_review", pn=pn, conflicts=record.conflicts)
        elif record.found:
            log.info(
                "scraped",
                pn=pn,
                qty=record.quantity,
                sale=str(record.sale_price),
                condition=record.condition_code,
                page_s=round(attempt.seconds, 2),
                interval_s=round(pace.current_interval, 1),
            )
        if pace.current_interval > settings.request_interval_seconds:
            log.warning(
                "pace_stretched",
                interval_s=round(pace.current_interval, 1),
                slowdown=round(pace.slowdown, 2),
            )

        if live is not None:
            live.record(output_row)
            if live.due:
                live.flush(conn)

    if live is not None:
        # Always flush the tail, including after a halt: the rows are already
        # in the database and the sheet should show what was actually done
        # before the stop.
        live.flush(conn)
        if live.abandoned or live.consecutive_failures:
            # The run was fine; the sheet is not. Said out loud because those
            # rows still show the previous cycle's prices, and a stale price
            # nobody knows is stale is the worst thing this system can produce.
            spans = ", ".join(f"{a}-{b}" for a, b in live.abandoned) or "the last block"
            detail = (
                f"the Output tab is behind the database. Rows not written: {spans}. "
                f"{live.failures} flush(es) failed. The database is complete; run "
                f"`txtav sheet-push` to rebuild the tab from it."
            )
            db.log_event(conn, "warning", "sheet_behind", detail)
            alerts.send(settings.alert_webhook, "TXT sheet is behind the database", detail)

    if report.halted is None:
        finish_cycle_if_complete(conn, cycle_id)

    log.info(
        "run_finished",
        **{
            k: getattr(report, k)
            for k in ("attempted", "success", "not_found", "failed", "manual_review")
        },
    )
    _notify(conn, settings, report)
    return report


def _notify(conn: sqlite3.Connection, settings: Settings, report: RunReport) -> None:
    """Raise the operator's attention when, and only when, it is warranted.

    A halt is loud because the run is stopped. A suspiciously empty run is
    equally loud: it looks identical to a healthy one in the database, and
    stale prices that read as current are worse than no prices at all.
    """
    if report.halted is not None:
        alerts.send(
            settings.alert_webhook,
            f"TXT run halted: {report.halted.kind}",
            f"{report.halted.detail}\n\n{report.summary()}",
        )
        return

    if report.offline:
        # Cut short because this machine lost its network: a near empty pass
        # is the expected result, and the alert could not leave anyway. A hung
        # browser is not excused the same way, since a standalone `txtav run`
        # has no supervisor to replace the browser and this alert is the signal.
        return

    usable = report.success + report.not_found
    if alerts.looks_empty(report.attempted, usable):
        detail = (
            f"{usable} of {report.attempted} parts produced a usable row. "
            f"Nothing errored, which is why this needs a look."
        )
        db.log_event(conn, "warning", "suspiciously_empty_run", detail)
        alerts.send(settings.alert_webhook, "TXT run returned little data", detail)


# A sheet that is configured but unreachable is raised at most this often. The
# run continues regardless, since the database is complete, but a pass starts
# every 40 minutes or so and one alert per pass would bury everything else.
SHEET_ALERT_EVERY_HOURS = 6


def _report_sheet_unavailable(conn: sqlite3.Connection, settings: Settings) -> None:
    """Say, loudly but not repeatedly, that results are not reaching the sheet.

    The run carries on: results land in the database either way and a
    `sheet-push` catches the sheet up. What must not happen is a fortnight of
    work landing nowhere the client looks while every log line reads healthy.
    """
    recent = conn.execute(
        "SELECT 1 FROM events WHERE kind = 'sheet_unavailable'"
        " AND ts > datetime('now', ?)",
        (f"-{SHEET_ALERT_EVERY_HOURS} hours",),
    ).fetchone()
    if recent is not None:
        return
    detail = (
        "results are being saved but not written to the Google Sheet: the "
        "Google token is missing, revoked, or cannot be refreshed. Run "
        "`txtav sheet-auth` by hand, then `txtav sheet-push` to catch up."
    )
    db.log_event(conn, "warning", "sheet_unavailable", detail)
    alerts.send(settings.alert_webhook, "TXT results are not reaching the sheet", detail)


def _save_page(directory: Path, pn: str, html: str) -> None:
    """Keep the page a row was built from, as evidence for review."""
    directory.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", pn)
    (directory / f"{safe}.html").write_text(html, encoding="utf-8")


@dataclass
class Attempt:
    """What came of fetching one part, across all its tries."""

    decision: guard.Decision | None = None
    record: extract.PartRecord | None = None
    error: str | None = None
    html: str | None = None
    seconds: float = 0.0
    # Every try timed out with no answer from the page at all. Two parts like
    # that in a row is the shape of a hung browser rather than a refusing
    # portal, and is answered by replacing the browser.
    timed_out: bool = False
    # This machine lost its network. The part is not at fault and must not be
    # marked failed, and the attempt says nothing about the portal either.
    offline: bool = False


def _backoff(settings: Settings, attempt: int) -> float:
    """Seconds to wait before retry number `attempt`, on top of the pacer.

    Exponential with full jitter: random between 0 and min(cap, base * 2^n).
    A random wait rather than an exact doubling keeps retries from lining up
    with whatever made the portal fail, which is the finding of AWS's study of
    backoff under contention.
    """
    ceiling = min(
        settings.backoff_cap_seconds,
        settings.backoff_base_seconds * (2 ** (attempt - 1)),
    )
    return random.uniform(0.0, ceiling) if ceiling > 0 else 0.0


def _attempt(
    navigator: Navigator,
    settings: Settings,
    pn: str,
    pace: Pacer,
    report: RunReport,
) -> Attempt:
    """Fetch and extract one part, retrying only ordinary transient faults.

    Every attempt waits on the pacer, retries included. The pacing is a promise
    about how hard we hit the portal, and a retry is just as much a request as
    a first try. Gating only first attempts meant that with roughly 60% of
    requests returning HTTP 500 and recovering on retry, the real rate was one
    request every 6.9 seconds rather than the 11 seconds promised.

    A retry also waits a backoff on top of the pacer, or the Retry-After the
    portal sent if it sent one. A stop decision is never retried.
    """
    last_error: str | None = None
    only_timeouts = True

    for attempt in range(1, settings.max_network_retries + 1):
        final = attempt == settings.max_network_retries
        pace.wait()
        report.requests += 1
        started = time.monotonic()
        try:
            fetched = navigator.fetch(pn)
        except Exception as exc:  # noqa: BLE001 - classified by the guard
            # A failed load still took time, and a run of slow failures is
            # exactly what should slow the pace down.
            pace.observe(time.monotonic() - started)
            decision = guard.assess_transport(exc)
            if decision.verdict is not guard.Verdict.RETRY:
                return Attempt(decision=decision, error=decision.detail)
            if decision.kind == "offline":
                # No retry can succeed until the connection is back, and each
                # one would only burn the part's attempts.
                log.warning("offline", pn=pn, detail=decision.detail)
                return Attempt(error=decision.detail, offline=True)
            last_error = decision.detail
            only_timeouts = only_timeouts and "timeout" in decision.detail.lower()
            log.warning("transport_retry", pn=pn, attempt=attempt, detail=decision.detail)
            if not final:
                time.sleep(_backoff(settings, attempt))
            continue

        only_timeouts = False  # the page answered, whatever it said
        pace.observe(fetched.seconds)

        if fetched.decision.should_stop:
            return Attempt(
                decision=fetched.decision,
                error=fetched.decision.detail,
                html=fetched.html,
                seconds=fetched.seconds,
            )

        if fetched.decision.verdict is guard.Verdict.RETRY:
            last_error = fetched.decision.detail
            if fetched.decision.kind == "server_error":
                report.server_errors += 1
            asked = guard.retry_after_seconds(fetched.retry_after)
            if asked is not None and asked > settings.retry_after_cap_seconds:
                # Asked to stay away longer than a run should sit unattended.
                # Waiting it out silently or retrying early are both wrong;
                # stopping for a person is the conservative reading.
                return Attempt(
                    decision=guard.Decision(
                        guard.Verdict.HALT,
                        "service_unavailable",
                        f"{last_error}; the portal asked us to wait {asked:.0f}s "
                        f"(Retry-After), longer than the "
                        f"{settings.retry_after_cap_seconds:.0f}s this run waits unattended",
                    ),
                    error=last_error,
                    html=fetched.html,
                    seconds=fetched.seconds,
                )
            log.warning(
                "page_retry",
                pn=pn,
                attempt=attempt,
                kind=fetched.decision.kind,
                detail=last_error,
                retry_after=asked,
            )
            if not final:
                time.sleep(asked if asked is not None else _backoff(settings, attempt))
            continue

        try:
            return Attempt(
                record=extract.extract(fetched.html, pn),
                html=fetched.html,
                seconds=fetched.seconds,
            )
        except (extract.ExtractionError, rules.FieldError) as exc:
            # The page loaded and the guard was happy, yet it is not a part
            # page, or a field we can see holds something we cannot read. That
            # is a changed site or a new state nobody has seen, and guessing at
            # it is how wrong prices reach the sheet.
            #
            # rules.FieldError belongs here and not only ExtractionError. Both
            # derive from ValueError but neither from the other, so catching
            # one alone let the other out of the run loop entirely: on
            # 2026-10-06 a heading reading "Your Price: Call for pricing" took
            # the whole process down with a traceback, leaving the part
            # in_flight, no halt recorded and no alert sent. A stop this
            # project cannot explain afterwards is worse than one it can.
            return Attempt(
                decision=guard.Decision(
                    guard.Verdict.HALT, "unrecognised_page", f"{pn}: {exc}"
                ),
                error=str(exc),
                html=fetched.html,
                seconds=fetched.seconds,
            )
        except Exception as exc:  # noqa: BLE001 - a halt beats dying silently
            # Anything else from the extractor is a bug in it rather than a
            # state of the portal, but the response has to be the same: stop,
            # write down why, and tell somebody. Letting it propagate loses all
            # three.
            return Attempt(
                decision=guard.Decision(
                    guard.Verdict.HALT,
                    "extractor_failed",
                    f"{pn}: {type(exc).__name__}: {exc}",
                ),
                error=str(exc),
                html=fetched.html,
                seconds=fetched.seconds,
            )

    return Attempt(
        error=last_error or "exhausted retries",
        timed_out=only_timeouts and last_error is not None,
    )


def save_failure_screenshot(
    navigator: Navigator, settings: Settings, pn: str, reason: str
) -> Path | None:
    """Photograph the page that caused a stop, with identities masked.

    Best effort: a browser that has already died cannot be photographed, and
    failing to capture evidence must not change the outcome of the stop.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", pn)
    path = Path(settings.screenshot_dir) / f"{int(time.time())}_{reason}_{safe}.png"
    try:
        counts = navigator.screenshot(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("screenshot_failed", pn=pn, error=f"{type(exc).__name__}: {exc}")
        return None

    # Checked per group, not on the total. A total above zero proves only that
    # something was hidden, and would stay healthy looking while one renamed
    # class leaked the same value into every image from then on.
    missed = redact_page.missed(counts)
    if missed:
        # The warning goes in the filename, not only the log. The runbook sends
        # an operator to this directory to look at images; nobody opens a JSON
        # log first, and a file that looks like every other screenshot will be
        # attached to a ticket without a second thought.
        flagged = path.with_name(
            "UNMASKED_" + "_".join(m.replace(" ", "-") for m in missed) + "_" + path.name
        )
        path.rename(flagged)
        path = flagged
        log.warning("screenshot_not_fully_masked", pn=pn, path=str(path), missed=missed)
    log.info("screenshot_saved", pn=pn, path=str(path), masked=counts)
    return path
