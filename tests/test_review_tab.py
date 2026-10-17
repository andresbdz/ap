"""The Review tab: where flagged parts can be seen without the database.

Asked for on 2026-10-17. Until then the flags lived only in the job store, so
nobody reading the Google Sheet could find the parts a decision was waiting on.
"""

from __future__ import annotations

import pytest

from txtav import db, exporter, live_sheet, sheets


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    rows = [
        ("A-1", 2, "more than one alternate part number is visible: X=1, Y=2"),
        ("B-1RX", 3, "part number suffix says new but page says 'EXCHANGE'"),
        ("C-1RT", 4, "unrecognised condition 'RENTAL' on page"),
        ("D-1", 5, "hidden price 1157.9 does not match the displayed Your Price 1312.12"),
        ("E-1", 6, None),
    ]
    with db.transaction(connection):
        connection.execute("INSERT INTO cycles(id) VALUES (1)")
        for pn, out_row, note in rows:
            connection.execute(
                "INSERT INTO parts(pn, dedupe_key, wh, source_row, output_row)"
                " VALUES (?, ?, 'TXT', ?, ?)",
                (pn, pn, out_row + 200, out_row),
            )
            connection.execute(
                "INSERT INTO results(cycle_id, pn, sale_price, conflict, conflict_note)"
                " VALUES (1, ?, '10.00', ?, ?)",
                (pn, int(note is not None), note),
            )
    yield connection
    connection.close()


class TestWhatTheTabHolds:
    def test_only_flagged_parts_most_urgent_first(self, conn):
        rows = exporter.review_rows(conn)
        assert [r[0] for r in rows] == ["D-1", "C-1RT", "B-1RX", "A-1"]

    def test_each_row_says_where_it_is_and_why(self, conn):
        first = dict(zip(exporter.REVIEW_COLUMNS, exporter.review_rows(conn)[0]))
        assert first["Row in Output"] == 5
        assert first["Reason"] == "Displayed price differs from the hidden price"
        assert "1312.12" in first["Detail"]

    def test_an_unknown_flag_is_still_listed(self):
        assert exporter.review_reason("something nobody has seen")[1] == "Other"


class _Call:
    def __init__(self, log, name, kwargs):
        self.log, self.name, self.kwargs = log, name, kwargs

    def execute(self):
        self.log.append((self.name, self.kwargs))
        if self.name == "get":
            return {"sheets": [{"properties": {"title": "Output", "sheetId": 0,
                    "gridProperties": {"rowCount": 10, "columnCount": 14}}}]
                    + ([{"properties": {"title": "Review", "sheetId": 9,
                        "gridProperties": {"rowCount": 1000, "columnCount": 26}}}]
                       if any(n == "batchUpdate" for n, _ in self.log[:-1]) else [])}
        return {"updatedCells": 0}


class _Values:
    def __init__(self, log):
        self.log = log

    def clear(self, **kw):
        return _Call(self.log, "clear", kw)

    def update(self, **kw):
        return _Call(self.log, "update", kw)


class _Spreadsheets:
    def __init__(self, log):
        self.log = log

    def get(self, **kw):
        return _Call(self.log, "get", kw)

    def batchUpdate(self, **kw):
        return _Call(self.log, "batchUpdate", kw)

    def values(self):
        return _Values(self.log)


class FakeService:
    def __init__(self):
        self.log = []

    def spreadsheets(self):
        return _Spreadsheets(self.log)


class TestWritingTheTab:
    def test_it_is_created_when_missing_and_never_touches_output(self, conn):
        service = FakeService()
        sheets.replace_tab(
            service, "sid", exporter.REVIEW_TAB, exporter.REVIEW_COLUMNS, exporter.review_rows(conn)
        )

        names = [name for name, _ in service.log]
        assert "batchUpdate" in names, "the Review tab was created"
        ranges = [kw.get("range", "") for _, kw in service.log]
        assert not any(r.startswith("Output") for r in ranges), "Output is never written"
        update = next(kw for name, kw in service.log if name == "update")
        assert update["range"] == "Review!A1:H5"
        assert update["body"]["values"][0] == list(exporter.REVIEW_COLUMNS)

    def test_a_failed_refresh_never_reaches_the_run(self, conn):
        class Broken:
            def spreadsheets(self):
                raise TimeoutError("timed out")

        live = live_sheet.LiveSheet(Broken(), "sid", "Output")
        assert live_sheet.refresh_review(live, conn) is False
