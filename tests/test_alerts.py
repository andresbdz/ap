"""Alerting and daily report tests."""

from __future__ import annotations

import sqlite3

import pytest

from txtav import alerts, db


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    connection = db.connect(tmp_path / "t.db")
    with db.transaction(connection):
        connection.executemany(
            "INSERT INTO parts(pn, dedupe_key, wh, source_row, output_row)"
            " VALUES (?,?,'TXT',?,?)",
            [(f"PN-{i}", f"PN-{i}", i + 200, i + 1) for i in range(1, 11)],
        )
        connection.execute("INSERT INTO cycles(id) VALUES (1)")
        statuses = (
            ["success"] * 4 + ["not_found"] * 2 + ["manual_review"]
            + ["failed"] + ["pending"] * 2
        )
        connection.executemany(
            "INSERT INTO jobs(cycle_id, pn, status) VALUES (1, ?, ?)",
            [(f"PN-{i}", status) for i, status in enumerate(statuses, start=1)],
        )
        connection.executemany(
            "INSERT INTO results(cycle_id, pn, availability) VALUES (1, ?, ?)",
            [("PN-1", "no_longer_available"), ("PN-2", "available")],
        )
    yield connection
    connection.close()


class TestDailyReport:
    def test_counts_every_status(self, conn):
        report = alerts.build_daily_report(conn)
        assert report.total_parts == 10
        assert report.success == 4
        assert report.not_found == 2
        assert report.manual_review == 1
        assert report.failed == 1
        assert report.pending == 2
        assert report.done == 8

    def test_counts_discontinued_parts_separately(self, conn):
        # These are a normal outcome, not a failure, and the client will want
        # to know how much of the list is no longer sold.
        assert alerts.build_daily_report(conn).discontinued == 1

    def test_percent_done(self, conn):
        assert alerts.build_daily_report(conn).percent_done == pytest.approx(80.0)

    def test_text_is_readable(self, conn):
        text = alerts.build_daily_report(conn).text()
        assert "progress" in text
        assert "8 of 10" in text

    def test_an_open_halt_is_the_headline(self, conn):
        db.raise_halt(conn, "rate_limited", "HTTP 429 on /PartsDetail/X")
        report = alerts.build_daily_report(conn)
        assert report.halt_kind == "rate_limited"
        assert "STOPPED: rate_limited" in report.text()

    def test_a_cleared_halt_is_not_reported(self, conn):
        halt_id = db.raise_halt(conn, "captcha", "x")
        db.clear_halt(conn, halt_id, "thanh")
        assert alerts.build_daily_report(conn).halt_kind is None

    def test_a_run_that_has_not_started_reports_zero_not_complete(self, tmp_path):
        # Regression: done was computed as total minus pending, and before a
        # cycle exists there are no job rows, so a list of 109,589 untouched
        # parts reported as 100% done.
        fresh = db.connect(tmp_path / "fresh.db")
        try:
            with db.transaction(fresh):
                fresh.executemany(
                    "INSERT INTO parts(pn, dedupe_key, wh, source_row, output_row)"
            " VALUES (?,?,'TXT',?,?)",
                    [(f"P{i}", f"P{i}", i + 200, i + 2) for i in range(5)],
                )
            report = alerts.build_daily_report(fresh)
            assert report.total_parts == 5
            assert report.done == 0
            assert report.percent_done == 0.0
        finally:
            fresh.close()

    def test_an_empty_store_does_not_divide_by_zero(self, tmp_path):
        empty = db.connect(tmp_path / "empty.db")
        try:
            assert alerts.build_daily_report(empty).percent_done == 0.0
        finally:
            empty.close()


class TestEmptyRunDetection:
    def test_a_healthy_run_is_not_flagged(self):
        assert not alerts.looks_empty(attempted=100, usable=95)

    def test_a_mostly_empty_run_is_flagged(self):
        # Stale prices that look current are worse than no prices, so this has
        # to be as loud as a crash.
        assert alerts.looks_empty(attempted=100, usable=10)

    def test_a_run_that_did_nothing_is_not_flagged(self):
        # Nothing attempted is a scheduling question, not a data question.
        assert not alerts.looks_empty(attempted=0, usable=0)

    def test_the_boundary(self):
        assert not alerts.looks_empty(attempted=100, usable=50)
        assert alerts.looks_empty(attempted=100, usable=49)


class TestSend:
    def test_no_webhook_is_not_an_error(self):
        assert alerts.send("", "title", "body") is False

    def test_a_failing_webhook_never_raises(self, monkeypatch):
        # An alerting outage must not take the run down with it.
        def boom(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(alerts.urllib.request, "urlopen", boom)
        assert alerts.send("https://example.invalid/hook", "t", "b") is False

    def test_a_successful_post_reports_true(self, monkeypatch):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(alerts.urllib.request, "urlopen", lambda *a, **k: Response())
        assert alerts.send("https://example.invalid/hook", "t", "b") is True
