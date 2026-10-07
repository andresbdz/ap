"""Keeping the Google Sheet roughly current while a run is in progress.

The client asked to see updates live rather than at the end of a 14 day cycle.
Rewriting all 109,589 rows for every few parts would be absurd, but it is not
needed: parts are processed in the order of their input tab, so the rows that
finish during a run are always a contiguous block. One request covers a few
hundred of them.

Failures here are logged and swallowed: the database is the source of truth and
taking a run down because a spreadsheet was briefly unreachable would cost far
more than a late update. But a failed block is remembered and folded into the
next write, because nothing else would ever go back for it. Each flush covers
the span that has just finished, so the following one addresses later rows; a
block dropped on failure would keep the previous cycle's prices in those rows
until someone ran a full `sheet-push`, and a stale price that reads as current
is the one outcome this project treats as worse than no price at all.
"""

from __future__ import annotations

import sqlite3

from txtav import exporter, logging_setup, sheets

log = logging_setup.get_logger("txtav.live_sheet")

# How many flushes a failed span is carried for before it is given up on. It has
# to be carried: each flush covers the rows that have just finished, so the next
# one addresses later rows and a dropped span is a hole nothing returns to. It
# also has to be bounded: a sheet unreachable for an hour would otherwise grow
# one span across thousands of rows and rewrite all of it on every attempt.
# Giving up is recorded, and the run says so at the end, because at that point
# only a full `sheet-push` will put those rows right.
MAX_CARRIED_FLUSHES = 5


class LiveSheet:
    """Flushes finished rows to the sheet in contiguous blocks."""

    def __init__(self, service, sheet_id: str, tab: str, *, every: int = 500) -> None:
        self.service = service
        self.sheet_id = sheet_id
        self.tab = tab
        self.every = max(1, every)
        self._pending: list[int] = []
        # The span a previous flush failed to write. Carried forward so the next
        # one covers it too, rather than leaving a hole nothing returns to.
        self._unwritten: tuple[int, int] | None = None
        self.flushes = 0
        self.failures = 0
        self.consecutive_failures = 0
        # Spans carried past MAX_CARRIED_FLUSHES and then given up on. Non empty
        # means the sheet is certainly stale in those rows until a full push.
        self.abandoned: list[tuple[int, int]] = []

    def record(self, output_row: int) -> None:
        self._pending.append(output_row)

    @property
    def due(self) -> bool:
        return len(self._pending) >= self.every

    def _span(self) -> tuple[int, int] | None:
        """The rows to write: what just finished, plus anything still unwritten."""
        span = None
        if self._pending:
            span = (min(self._pending), max(self._pending))
            self._pending.clear()
        if self._unwritten is not None:
            span = (
                self._unwritten
                if span is None
                else (min(span[0], self._unwritten[0]), max(span[1], self._unwritten[1]))
            )
        return span

    def flush(self, conn: sqlite3.Connection) -> int:
        """Write the pending span. Returns cells written, 0 if nothing or failed."""
        span = self._span()
        if span is None:
            return 0
        first, last = span

        try:
            rows = exporter.rows_in_range(conn, first, last)
            if not rows:
                self._unwritten = None
                return 0
            cells = sheets.write_range(
                self.service, self.sheet_id, self.tab, rows, first_row=first
            )
        except Exception as exc:  # noqa: BLE001 - never take the run down for this
            self.failures += 1
            self.consecutive_failures += 1
            carry = self.consecutive_failures < MAX_CARRIED_FLUSHES
            self._unwritten = (first, last) if carry else None
            if not carry:
                self.abandoned.append((first, last))
            log.warning(
                "live_sheet_flush_failed",
                first_row=first,
                last_row=last,
                retrying_next_flush=carry,
                consecutive=self.consecutive_failures,
                error=f"{type(exc).__name__}: {exc}",
            )
            if not carry:
                log.error(
                    "live_sheet_rows_abandoned",
                    first_row=first,
                    last_row=last,
                    detail="these rows still show the previous cycle; "
                    "run `txtav sheet-push` to rebuild the tab",
                )
            return 0

        self._unwritten = None
        self.flushes += 1
        self.consecutive_failures = 0
        return cells


def build(settings) -> LiveSheet | None:
    """A LiveSheet when the sheet is configured and reachable, otherwise None.

    A run must still work with no spreadsheet at all: the results are in the
    database either way, and `sheet-push` can catch the sheet up later.
    """
    if not settings.sheet_id:
        return None
    try:
        creds = sheets.authorise(
            settings.google_client_secrets,
            settings.google_token,
            open_browser=False,
            interactive=False,
        )
        service = sheets.build_service(creds)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "live_sheet_unavailable", error=f"{type(exc).__name__}: {exc}"
        )
        return None
    return LiveSheet(service, settings.sheet_id, settings.sheet_tab)
