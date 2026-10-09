"""Live sheet updates during a run.

Two things matter: the write covers exactly the rows that finished, and a
spreadsheet problem never takes the run down. The database is the source of
truth; the sheet is a projection that a later flush or `sheet-push` repairs.
"""

from __future__ import annotations

import sqlite3

import pytest

from txtav import db, live_sheet


class RecordingSheets:
    """Stands in for txtav.sheets, recording the write and optionally failing."""

    def __init__(self, fail: Exception | None = None):
        self.writes: list[tuple[int, int]] = []
        self.fail = fail

    def write_range(self, service, sheet_id, tab, rows, first_row):
        if self.fail is not None:
            raise self.fail
        self.writes.append((first_row, len(rows)))
        return len(rows) * 11


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    connection = db.connect(tmp_path / "t.db")
    with db.transaction(connection):
        connection.executemany(
            "INSERT INTO parts(pn, dedupe_key, wh, source_row, output_row)"
            " VALUES (?,?,'TXT',?,?)",
            [(f"PN-{i}", f"PN-{i}", i + 200, i) for i in range(2, 12)],
        )
        connection.execute("INSERT INTO cycles(id) VALUES (1)")
        connection.executemany(
            "INSERT INTO results(cycle_id, pn, sale_price, availability)"
            " VALUES (1, ?, '1.00', 'available')",
            [(f"PN-{i}",) for i in range(2, 12)],
        )
    yield connection
    connection.close()


@pytest.fixture
def recorder(monkeypatch) -> RecordingSheets:
    fake = RecordingSheets()
    monkeypatch.setattr(live_sheet.sheets, "write_range", fake.write_range)
    return fake


class TestBatching:
    def test_nothing_is_written_before_the_batch_is_full(self, conn, recorder):
        sheet = live_sheet.LiveSheet(None, "sid", "Output", every=5)
        for row in (2, 3, 4):
            sheet.record(row)
        assert not sheet.due
        assert recorder.writes == []

    def test_a_full_batch_is_due(self, conn, recorder):
        sheet = live_sheet.LiveSheet(None, "sid", "Output", every=3)
        for row in (2, 3, 4):
            sheet.record(row)
        assert sheet.due

    def test_the_write_starts_at_the_first_pending_row(self, conn, recorder):
        sheet = live_sheet.LiveSheet(None, "sid", "Output", every=3)
        for row in (4, 5, 6):
            sheet.record(row)
        sheet.flush(conn)
        assert recorder.writes == [(4, 3)]

    def test_flushing_twice_does_not_rewrite_the_first_batch(self, conn, recorder):
        sheet = live_sheet.LiveSheet(None, "sid", "Output", every=2)
        sheet.record(2)
        sheet.record(3)
        sheet.flush(conn)
        sheet.record(4)
        sheet.record(5)
        sheet.flush(conn)
        assert recorder.writes == [(2, 2), (4, 2)]

    def test_flushing_nothing_is_a_no_op(self, conn, recorder):
        sheet = live_sheet.LiveSheet(None, "sid", "Output", every=5)
        assert sheet.flush(conn) == 0
        assert recorder.writes == []


class TestFailureIsNotFatal:
    def test_a_sheet_error_is_swallowed_and_counted(self, conn, monkeypatch):
        # Taking a 14 day run down because a spreadsheet was briefly
        # unreachable would cost far more than a missed live update.
        failing = RecordingSheets(fail=RuntimeError("503 backend error"))
        monkeypatch.setattr(live_sheet.sheets, "write_range", failing.write_range)

        sheet = live_sheet.LiveSheet(None, "sid", "Output", every=2)
        sheet.record(2)
        sheet.record(3)

        assert sheet.flush(conn) == 0
        assert sheet.failures == 1

    def test_a_failed_span_is_carried_into_the_next_flush(self, conn, monkeypatch):
        # Each flush covers the rows that have just finished, so the next one
        # addresses later rows. A span dropped on failure is a hole nothing ever
        # returns to, and those rows keep the previous cycle's prices for the
        # rest of the fortnight while looking exactly like current ones.
        recorder = RecordingSheets(fail=RuntimeError("503"))
        monkeypatch.setattr(live_sheet.sheets, "write_range", recorder.write_range)
        sheet = live_sheet.LiveSheet(None, "sid", "Output", every=2)

        sheet.record(2)
        sheet.record(3)
        assert sheet.flush(conn) == 0

        recorder.fail = None  # the sheet comes back
        sheet.record(4)
        sheet.flush(conn)

        assert recorder.writes[-1] == (2, 3), "rows 2 to 4, not just the new one"

    def test_a_carried_span_is_eventually_given_up_on(self, conn, monkeypatch):
        # Bounded, because a sheet unreachable for an hour would otherwise grow
        # one span across thousands of rows and rewrite all of it every time.
        failing = RecordingSheets(fail=RuntimeError("503"))
        monkeypatch.setattr(live_sheet.sheets, "write_range", failing.write_range)
        sheet = live_sheet.LiveSheet(None, "sid", "Output", every=1)

        sheet.record(2)
        for _ in range(live_sheet.MAX_CARRIED_FLUSHES):
            sheet.flush(conn)

        assert sheet.abandoned == [(2, 2)], "the span the operator has to repair"
        assert sheet.flush(conn) == 0, "and nothing is carried any further"
        assert sheet.failures == live_sheet.MAX_CARRIED_FLUSHES


class TestAnUnwrittenSpanOutlivesThePass:
    """A LiveSheet lasts one pass. What it failed to write must not die with it.

    On 2026-10-09 the end of pass flush timed out at Google, the next pass built
    a fresh LiveSheet with nothing to carry, and rows 1402 to 1601 stayed stale
    until a manual push.
    """

    def test_the_next_pass_writes_what_this_one_could_not(self, conn, monkeypatch):
        recorder = RecordingSheets(fail=TimeoutError("timed out"))
        monkeypatch.setattr(live_sheet.sheets, "write_range", recorder.write_range)

        first_pass = live_sheet.LiveSheet(None, "sid", "Output")
        first_pass.record(2)
        first_pass.record(3)
        first_pass.flush(conn)

        recorder.fail = None
        next_pass = live_sheet.LiveSheet(None, "sid", "Output")  # a new pass, or a new process
        next_pass.record(4)
        next_pass.flush(conn)

        assert recorder.writes == [(2, 3)], "rows 2 to 4 in one write, nothing lost"

    def test_it_is_written_even_if_the_next_pass_finishes_nothing(self, conn, monkeypatch):
        recorder = RecordingSheets(fail=TimeoutError("timed out"))
        monkeypatch.setattr(live_sheet.sheets, "write_range", recorder.write_range)
        failed = live_sheet.LiveSheet(None, "sid", "Output")
        failed.record(5)
        failed.flush(conn)

        recorder.fail = None
        live_sheet.LiveSheet(None, "sid", "Output").flush(conn)

        assert recorder.writes == [(5, 1)]

    def test_a_successful_write_forgets_it(self, conn, monkeypatch):
        recorder = RecordingSheets(fail=TimeoutError("timed out"))
        monkeypatch.setattr(live_sheet.sheets, "write_range", recorder.write_range)
        sheet = live_sheet.LiveSheet(None, "sid", "Output")
        sheet.record(2)
        sheet.flush(conn)
        recorder.fail = None
        sheet.flush(conn)

        assert live_sheet.load_unwritten(conn) == (None, 0)

    def test_the_bound_counts_failures_across_passes(self, conn, monkeypatch):
        # Otherwise every pass would start again at zero and a long outage would
        # grow one span without end.
        monkeypatch.setattr(
            live_sheet.sheets, "write_range", RecordingSheets(fail=TimeoutError("x")).write_range
        )
        last = None
        for _ in range(live_sheet.MAX_CARRIED_FLUSHES):
            last = live_sheet.LiveSheet(None, "sid", "Output")
            last.record(2)
            last.flush(conn)

        assert last.abandoned == [(2, 2)]
        assert live_sheet.load_unwritten(conn) == (None, 0)


class TestBuild:
    def test_no_sheet_configured_means_no_live_updates(self):
        from txtav.config import Settings

        assert live_sheet.build(Settings(sheet_id="")) is None

    def test_an_auth_failure_does_not_raise(self, monkeypatch):
        from txtav.config import Settings

        def boom(*args, **kwargs):
            raise RuntimeError("no token")

        monkeypatch.setattr(live_sheet.sheets, "authorise", boom)
        assert live_sheet.build(Settings(sheet_id="sid")) is None
