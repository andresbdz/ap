"""What changes when the run moves to the machine that will carry it.

That machine is reached only by remote desktop and left alone for a fortnight,
so the failures that matter are the quiet ones: a wait with no timeout, a crash
in a terminal nobody is watching, a database created empty in the wrong folder,
a cycle that starts out days late.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from txtav import commands, db, preflight, runner, sheets, supervisor
from txtav.config import Settings


class TestTheSheetNeverWaitsForAPerson:
    """The consent flow waits for a browser callback with no timeout at all.

    Reached from inside a run, a missing token would stop the cycle on that
    wait forever. Forgetting to copy secrets/ to the new machine is enough.
    """

    def test_a_missing_token_raises_instead_of_waiting(self, tmp_path, monkeypatch):
        client = tmp_path / "client.json"
        client.write_text("{}")

        def must_not_run(*args, **kwargs):
            raise AssertionError("started the interactive consent flow")

        monkeypatch.setattr(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
            must_not_run,
        )
        with pytest.raises(sheets.SheetError, match="sheet-auth"):
            sheets.authorise(client, tmp_path / "absent-token.json", interactive=False)

    def test_the_live_sheet_asks_non_interactively(self, monkeypatch):
        from txtav import live_sheet

        seen = {}

        def capture(*args, **kwargs):
            seen.update(kwargs)
            raise sheets.SheetError("no token")

        monkeypatch.setattr(live_sheet.sheets, "authorise", capture)
        assert live_sheet.build(Settings(sheet_id="sid")) is None
        assert seen.get("interactive") is False


class TestAnUnreachableSheetIsSaidOutLoud:
    def _conn(self, tmp_path) -> sqlite3.Connection:
        return db.connect(tmp_path / "t.db")

    def test_it_alerts_once_then_stays_quiet(self, tmp_path, monkeypatch):
        sent = []
        monkeypatch.setattr(runner.alerts, "send", lambda hook, title, body: sent.append(title))
        conn = self._conn(tmp_path)
        settings = Settings(sheet_id="sid")

        runner._report_sheet_unavailable(conn, settings)
        runner._report_sheet_unavailable(conn, settings)

        assert len(sent) == 1, "a pass every 40 minutes must not mean an alert every 40"
        events = conn.execute(
            "SELECT COUNT(*) n FROM events WHERE kind = 'sheet_unavailable'"
        ).fetchone()["n"]
        assert events == 1
        conn.close()


class TestTheSupervisorNeverDiesSilently:
    """The browser now relaunches every few hours with nobody watching."""

    def test_a_browser_that_will_not_launch_is_reported(self, tmp_path, monkeypatch):
        sent = []
        monkeypatch.setattr(
            supervisor.alerts, "send", lambda hook, title, body: sent.append(title)
        )
        monkeypatch.setattr(supervisor.Stopper, "install", lambda self: None)

        class WillNotLaunch:
            def __enter__(self):
                raise RuntimeError("Executable doesn't exist at /ms-playwright/chromium")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(supervisor, "browser_session", lambda *a, **k: WillNotLaunch())
        settings = Settings(db_path=tmp_path / "t.db")
        conn = db.connect(settings.db_path)

        report = supervisor.supervise(conn, settings, sleep=lambda s: None)

        assert report.stopped_because.startswith("crashed")
        assert sent == ["TXT supervisor stopped unexpectedly"]
        assert conn.execute(
            "SELECT COUNT(*) n FROM events WHERE kind = 'supervisor_crashed'"
        ).fetchone()["n"] == 1
        conn.close()


class TestTheCycleWindowCanStartWhenTheRealRunDoes:
    def _store(self, tmp_path) -> sqlite3.Connection:
        conn = db.connect(tmp_path / "t.db")
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO parts(pn, dedupe_key, wh, source_row, output_row)"
                " VALUES ('A-1', 'A-1', 'TXT', 2, 2), ('A-2', 'A-2', 'TXT', 3, 3)"
            )
            conn.execute(
                "INSERT INTO cycles(id, started_at, ends_at) VALUES"
                " (1, '2026-10-02 12:19:11', '2026-10-16 12:19:11')"
            )
            conn.execute(
                "INSERT INTO jobs(cycle_id, pn, status) VALUES"
                " (1, 'A-1', 'success'), (1, 'A-2', 'pending')"
            )
            conn.execute(
                "INSERT INTO results(cycle_id, pn, sale_price) VALUES (1, 'A-1', '9.99')"
            )
        return conn

    def test_only_the_dates_move(self, tmp_path):
        conn = self._store(tmp_path)

        started, ends = runner.restart_cycle_window(conn, 1, 14)

        assert started > "2026-10-02 12:19:11"
        statuses = dict(conn.execute("SELECT pn, status FROM jobs").fetchall())
        assert statuses == {"A-1": "success", "A-2": "pending"}, "no work is redone"
        assert conn.execute("SELECT sale_price FROM results").fetchone()[0] == "9.99"
        days = conn.execute(
            "SELECT julianday(ends_at) - julianday(started_at) FROM cycles"
        ).fetchone()[0]
        assert abs(days - 14) < 1e-6
        conn.close()

    def test_a_finished_cycle_is_left_alone(self, tmp_path):
        conn = self._store(tmp_path)
        conn.execute("UPDATE cycles SET status = 'finished'")
        with pytest.raises(ValueError, match="not running"):
            runner.restart_cycle_window(conn, 1, 14)
        conn.close()


class TestTheWrongDirectoryIsRefused:
    """Every path is relative to where the command starts."""

    def test_no_database_is_refused_and_none_is_created(self, tmp_path, capsys):
        settings = Settings(db_path=tmp_path / "data" / "txtav.db")

        assert commands._refuse_empty_store(settings) is True
        assert not settings.db_path.exists(), "the check must not create one"
        assert "not the project directory" in capsys.readouterr().out

    def test_a_store_with_parts_is_accepted(self, tmp_path):
        settings = Settings(db_path=tmp_path / "t.db")
        conn = db.connect(settings.db_path)
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO parts(pn, dedupe_key, wh, source_row, output_row)"
                " VALUES ('A-1', 'A-1', 'TXT', 2, 2)"
            )
        conn.close()
        assert commands._refuse_empty_store(settings) is False


class TestPreflight:
    def test_an_old_python_fails(self):
        assert preflight.check_python((3, 11, 9)).status == preflight.FAIL
        assert preflight.check_python((3, 12, 0)).status == preflight.PASS

    def test_a_missing_package_is_named(self):
        def importer(name):
            if name == "googleapiclient":
                raise ImportError(name)

        check = preflight.check_packages(importer)
        assert check.status == preflight.FAIL
        assert "google-api-python-client" in check.detail

    def test_no_env_file_fails(self, tmp_path):
        assert preflight.check_env_file(tmp_path / ".env").status == preflight.FAIL

    def test_a_missing_store_fails_without_being_created(self, tmp_path):
        path = tmp_path / "txtav.db"
        [check] = preflight.check_store(path)
        assert check.status == preflight.FAIL
        assert not path.exists()

    def test_an_open_halt_fails(self, tmp_path):
        path = tmp_path / "t.db"
        conn = db.connect(path)
        db.raise_halt(conn, "forbidden", "HTTP 403")
        conn.close()

        checks = {c.name: c for c in preflight.check_store(path)}
        assert checks["halt"].status == preflight.FAIL

    def test_a_late_cycle_points_at_cycle_restart(self, tmp_path):
        path = tmp_path / "t.db"
        conn = db.connect(path)
        with db.transaction(conn):
            conn.executemany(
                "INSERT INTO parts(pn, dedupe_key, wh, source_row, output_row)"
                " VALUES (?, ?, 'TXT', ?, ?)",
                [(f"P{i}", f"P{i}", i, i) for i in range(2, 1002)],
            )
            conn.execute(
                "INSERT INTO cycles(id, started_at, ends_at) VALUES"
                " (1, datetime('now', '-7 days'), datetime('now', '+7 days'))"
            )
            conn.execute("INSERT INTO jobs(cycle_id, pn) SELECT 1, pn FROM parts")
        conn.close()

        checks = {c.name: c for c in preflight.check_store(path)}
        assert checks["cycle"].status == preflight.WARN
        assert "cycle-restart" in checks["cycle"].detail

    def test_no_webhook_is_a_warning_not_a_failure(self):
        assert preflight.check_alerts("").status == preflight.WARN
        assert preflight.check_alerts("https://hooks.example/x").status == preflight.PASS

    def test_low_disk_warns(self, tmp_path):
        class Usage:
            free = 1 * 1024**3

        check = preflight.check_disk(tmp_path, usage=lambda p: Usage())
        assert check.status == preflight.WARN
