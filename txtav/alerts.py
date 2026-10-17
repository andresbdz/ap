"""Notifications, and the daily progress report they are built from.

Three things have to reach a person quickly:

- a halt, because the run is stopped until someone clears it by hand
- an expired session, because only a human can supply the 2FA code
- a run that returned far fewer usable rows than it should have, because a
  silently empty run looks exactly like a successful one in the database

The webhook is optional. With none configured everything still lands in the
structured log, which is where the post mortem of any stoppage comes from.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date

from txtav import db, logging_setup

log = logging_setup.get_logger("txtav.alerts")

# Below this share of attempted parts yielding a usable row, a run is treated
# as suspect even though nothing errored. A long tail of discontinued parts is
# normal; two thirds of a run coming back empty is not.
EMPTY_RUN_THRESHOLD = 0.5


@dataclass
class DailyReport:
    day: str
    cycle_id: int | None
    total_parts: int
    done: int
    success: int
    not_found: int
    discontinued: int
    quote_only: int
    not_offered: int
    failed: int
    manual_review: int
    pending: int
    halt_kind: str | None
    halt_detail: str | None
    schedule: str | None = None
    # Parts with an actual price. Not the same as `success`, which also counts
    # a quote only or not offered part read correctly: labelling that number
    # "priced" overstated coverage by 353 parts on 2026-10-09.
    priced: int | None = None
    rental: int = 0

    @property
    def percent_done(self) -> float:
        return 100 * self.done / self.total_parts if self.total_parts else 0.0

    def text(self) -> str:
        lines = [
            f"TXT parts run, {self.day}",
            f"  progress      {self.done:,} of {self.total_parts:,} ({self.percent_done:.1f}%)",
            f"  priced        {self.success if self.priced is None else self.priced:,}",
            f"  discontinued  {self.discontinued:,}",
            f"  quote only    {self.quote_only:,}",
            f"  not offered   {self.not_offered:,}",
            f"  rental        {self.rental:,}",
            f"  not found     {self.not_found:,}",
            f"  for review    {self.manual_review:,}",
            f"  failed        {self.failed:,}",
            f"  remaining     {self.pending:,}",
        ]
        if self.schedule:
            lines.append(f"  schedule      {self.schedule}")
        if self.halt_kind:
            lines += [
                "",
                f"  STOPPED: {self.halt_kind}",
                f"  {self.halt_detail}",
                "  No further parts will be looked up until this is cleared.",
            ]
        return "\n".join(lines)


def build_daily_report(conn: sqlite3.Connection) -> DailyReport:
    cycle = conn.execute(
        "SELECT id FROM cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    cycle_id = int(cycle["id"]) if cycle else None

    counts: dict[str, int] = {}
    if cycle_id is not None:
        for row in conn.execute(
            "SELECT status, COUNT(*) n FROM jobs WHERE cycle_id = ? GROUP BY status",
            (cycle_id,),
        ):
            counts[row["status"]] = int(row["n"])

    # Counted and reported rather than flagged: both are ordinary business
    # states, but the client should see how much of their list has no price.
    by_availability: dict[str, int] = {}
    if cycle_id is not None:
        for row in conn.execute(
            "SELECT availability, COUNT(*) n FROM results WHERE cycle_id = ?"
            " GROUP BY availability",
            (cycle_id,),
        ):
            by_availability[row["availability"]] = int(row["n"])
    discontinued = by_availability.get("no_longer_available", 0)
    quote_only = by_availability.get("price_on_request", 0)
    not_offered = by_availability.get("no_price_listed", 0)
    rental = by_availability.get("rental", 0)
    priced = 0
    if cycle_id is not None:
        priced = int(
            conn.execute(
                "SELECT COUNT(*) n FROM results WHERE cycle_id = ?"
                " AND availability = 'available' AND sale_price IS NOT NULL",
                (cycle_id,),
            ).fetchone()["n"]
        )

    total = int(conn.execute("SELECT COUNT(*) n FROM parts").fetchone()["n"])
    pending = counts.get("pending", 0) + counts.get("in_flight", 0)
    # Counted from the finished statuses rather than as total minus pending.
    # Before a cycle exists there are no job rows at all, and subtracting would
    # report a run that has not started as 100% complete.
    done = sum(counts.get(status, 0) for status in db.TERMINAL_STATUSES)
    halt = conn.execute(
        "SELECT kind, detail FROM halts WHERE cleared_at IS NULL ORDER BY id LIMIT 1"
    ).fetchone()

    schedule = None
    if cycle_id is not None:
        from txtav import runner

        progress = runner.cycle_progress(conn, cycle_id)
        if progress is not None:
            schedule = (
                "on track"
                if progress.on_schedule
                else f"BEHIND by {progress.behind_by:,} parts"
            ) + f", cycle ends {progress.ends_at}"

    return DailyReport(
        day=date.today().isoformat(),
        cycle_id=cycle_id,
        total_parts=total,
        done=done,
        success=counts.get("success", 0),
        not_found=counts.get("not_found", 0),
        discontinued=discontinued,
        quote_only=quote_only,
        not_offered=not_offered,
        rental=rental,
        failed=counts.get("failed", 0),
        manual_review=counts.get("manual_review", 0),
        pending=pending,
        halt_kind=halt["kind"] if halt else None,
        halt_detail=halt["detail"] if halt else None,
        schedule=schedule,
        priced=priced,
    )


def looks_empty(attempted: int, usable: int) -> bool:
    """True when a run completed but produced suspiciously little.

    Stale prices that look current are worse than no prices, so a run that
    quietly stopped returning data has to be as loud as one that crashed.
    """
    if attempted == 0:
        return False
    return (usable / attempted) < EMPTY_RUN_THRESHOLD


def send(webhook: str, title: str, body: str, *, timeout: int = 15) -> bool:
    """Post to a webhook. Never raises: a failed alert must not kill a run."""
    text = f"*{title}*\n```\n{body}\n```"
    if not webhook:
        log.info("alert_not_sent_no_webhook", title=title)
        return False

    payload = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(
        webhook, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ok = 200 <= response.status < 300
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log.warning("alert_failed", title=title, error=f"{type(exc).__name__}: {exc}")
        return False

    log.info("alert_sent", title=title, ok=ok)
    return ok
