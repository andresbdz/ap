"""Keeping the Google Sheet roughly current while a run is in progress.

The client asked to see updates live rather than at the end of a 14 day cycle.
Rewriting all 109,589 rows for every few parts would be absurd, but it is not
needed: parts are processed in the order of their input tab, so the rows that
finish during a run are always a contiguous block. One request covers a few
hundred of them.

Failures here are logged and swallowed. The database is the source of truth and
the sheet is a projection of it; losing a live update costs nothing, because
the next flush or a `sheet-push` rewrites the same rows. Taking a run down
because a spreadsheet was briefly unreachable would cost a great deal more.
"""

from __future__ import annotations

import sqlite3

from txtav import exporter, logging_setup, sheets

log = logging_setup.get_logger("txtav.live_sheet")


class LiveSheet:
    """Flushes finished rows to the sheet in contiguous blocks."""

    def __init__(self, service, sheet_id: str, tab: str, *, every: int = 500) -> None:
        self.service = service
        self.sheet_id = sheet_id
        self.tab = tab
        self.every = max(1, every)
        self._pending: list[int] = []
        self.flushes = 0
        self.failures = 0

    def record(self, source_row: int) -> None:
        self._pending.append(source_row)

    @property
    def due(self) -> bool:
        return len(self._pending) >= self.every

    def flush(self, conn: sqlite3.Connection) -> int:
        """Write the pending span. Returns cells written, 0 if nothing or failed."""
        if not self._pending:
            return 0

        first, last = min(self._pending), max(self._pending)
        self._pending.clear()

        try:
            rows = exporter.rows_in_range(conn, first, last)
            if not rows:
                return 0
            # source_row 1 is the input tab's header, and the output tab's
            # header is row 1 as well, so the two line up directly.
            cells = sheets.write_range(
                self.service, self.sheet_id, self.tab, rows, first_row=first
            )
            self.flushes += 1
            return cells
        except Exception as exc:  # noqa: BLE001 - never take the run down for this
            self.failures += 1
            log.warning(
                "live_sheet_flush_failed",
                first_row=first,
                last_row=last,
                error=f"{type(exc).__name__}: {exc}",
            )
            return 0


def build(settings) -> LiveSheet | None:
    """A LiveSheet when the sheet is configured and reachable, otherwise None.

    A run must still work with no spreadsheet at all: the results are in the
    database either way, and `sheet-push` can catch the sheet up later.
    """
    if not settings.sheet_id:
        return None
    try:
        creds = sheets.authorise(
            settings.google_client_secrets, settings.google_token, open_browser=False
        )
        service = sheets.build_service(creds)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "live_sheet_unavailable", error=f"{type(exc).__name__}: {exc}"
        )
        return None
    return LiveSheet(service, settings.sheet_id, settings.sheet_tab)
