"""Supervisor tests.

What matters here is when it stops. Continuing past a halt is the failure this
whole project is built to avoid, so each stop condition gets its own test.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time as tod

import pytest

from txtav import db, guard, supervisor
from txtav.config import Settings


class FakeNavigator:
    def __init__(self, signed_in: bool = True):
        self.signed_in = signed_in
        self.checks = 0

    def is_signed_in(self) -> bool:
        self.checks += 1
        return self.signed_in


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "t.db", alert_webhook="")


@pytest.fixture
def conn(settings) -> sqlite3.Connection:
    connection = db.connect(settings.db_path)
    with db.transaction(connection):
        connection.executemany(
            "INSERT INTO parts(pn, dedupe_key, wh, source_row) VALUES (?,?,'TXT',?)",
            [(f"PN-{i}", f"PN-{i}", i) for i in range(1, 4)],
        )
    yield connection
    connection.close()


@pytest.fixture
def harness(monkeypatch):
    """Replaces the browser and the run loop; records what was asked of them."""
    state = {
        "navigator": FakeNavigator(),
        "runs": [],
        "slept": [],
        "alerts": [],
        "results": [],
    }

    class FakeSession:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(supervisor, "browser_session", lambda *a, **k: FakeSession())
    monkeypatch.setattr(supervisor, "Navigator", lambda page, base: state["navigator"])
    monkeypatch.setattr(
        supervisor.alerts, "send", lambda hook, title, body: state["alerts"].append(title)
    )

    def fake_run(conn, navigator, settings, **kwargs):
        state["runs"].append(1)
        if state["results"]:
            return state["results"].pop(0)
        return supervisor.runner.RunReport(attempted=3, success=3)

    monkeypatch.setattr(supervisor.runner, "run", fake_run)
    return state


def sleeper(state):
    return state["slept"].append


class TestStoppingConditions:
    def test_an_open_halt_stops_before_any_work(self, conn, settings, harness):
        db.raise_halt(conn, "rate_limited", "HTTP 429")
        report = supervisor.supervise(conn, settings, sleep=sleeper(harness))

        assert report.passes == 0
        assert "halted: rate_limited" in report.stopped_because
        assert harness["runs"] == []

    def test_a_halt_raised_mid_run_ends_the_loop(self, conn, settings, harness):
        harness["results"] = [
            supervisor.runner.RunReport(
                attempted=1,
                halted=guard.Decision(guard.Verdict.HALT, "captcha", "saw a captcha"),
            )
        ]
        report = supervisor.supervise(conn, settings, sleep=sleeper(harness))

        assert report.passes == 1
        assert "halted: captcha" in report.stopped_because

    def test_an_expired_session_stops_and_alerts(self, conn, settings, harness):
        harness["navigator"] = FakeNavigator(signed_in=False)
        report = supervisor.supervise(conn, settings, sleep=sleeper(harness))

        assert report.stopped_because == "session expired"
        assert harness["runs"] == []
        assert harness["alerts"] == ["TXT session expired"]

    def test_a_halt_alerts_once_and_does_not_loop(self, conn, settings, harness):
        db.raise_halt(conn, "captcha", "saw a captcha")
        supervisor.supervise(conn, settings, sleep=sleeper(harness))
        assert len(harness["alerts"]) == 1


class TestEachPassIsBounded:
    """The loop only regains control between passes.

    Measured on 2026-10-06: a supervise started with --max-hours 1.5 was still
    going at 2.5 hours, because its one pass had taken the whole queue of
    109,351 parts. SIGINT and SIGTERM were recorded and never acted on for the
    same reason, and neither was the session checked again in all that time.
    """

    def test_a_limit_is_passed_to_every_run(self, conn, settings, monkeypatch, harness):
        seen: list[int | None] = []

        def capture(conn, navigator, settings, **kwargs):
            seen.append(kwargs.get("limit"))
            return supervisor.runner.RunReport(attempted=1, success=1)

        monkeypatch.setattr(supervisor.runner, "run", capture)
        supervisor.supervise(
            conn, settings, max_passes=3, pass_parts=200, sleep=sleeper(harness)
        )

        assert seen == [200, 200, 200]

    def test_the_default_pass_is_small_enough_to_recheck_often(self):
        # At 11 seconds a part, a pass must be short enough that an expired
        # session or a stop request is noticed in minutes, not days.
        minutes = supervisor.PASS_PARTS * 11 / 60
        assert minutes < 60, f"a pass would run {minutes:.0f} minutes before any check"

    def test_the_session_is_checked_once_per_pass(self, conn, settings, harness):
        supervisor.supervise(conn, settings, max_passes=3, sleep=sleeper(harness))
        assert harness["navigator"].checks == 3


class TestLimits:
    def test_stops_after_the_requested_passes(self, conn, settings, harness):
        report = supervisor.supervise(
            conn, settings, max_passes=3, sleep=sleeper(harness)
        )
        assert report.passes == 3
        assert report.parts == 9
        assert report.stopped_because == "reached the pass limit"

    def test_stops_after_the_requested_hours(self, conn, settings, harness):
        report = supervisor.supervise(
            conn, settings, max_hours=0.0, sleep=sleeper(harness)
        )
        assert report.passes == 0
        assert report.stopped_because == "reached the time limit"


class TestIdling:
    def test_an_empty_queue_waits_rather_than_spinning(self, conn, settings, harness):
        harness["results"] = [supervisor.runner.RunReport(attempted=0)]
        supervisor.supervise(conn, settings, max_passes=2, sleep=sleeper(harness))

        assert supervisor.IDLE_SLEEP_SECONDS in harness["slept"]

    def test_outside_the_operating_window_it_waits_without_running(
        self, conn, settings, harness
    ):
        settings.window_start = tod(8, 0)
        settings.window_end = tod(20, 0)
        report = supervisor.supervise(
            conn,
            settings,
            max_hours=0.0,
            sleep=sleeper(harness),
            now=lambda: datetime(2026, 10, 6, 3, 0),
        )
        assert harness["runs"] == []
        assert report.passes == 0


class TestCleanShutdown:
    def test_a_stop_request_ends_the_loop_at_a_pass_boundary(
        self, conn, settings, harness, monkeypatch
    ):
        # Killing mid part is survivable, since the job commits per part, but
        # stopping between passes makes a deliberate shutdown distinguishable
        # from a crash in the logs.
        original = supervisor.Stopper.install
        monkeypatch.setattr(supervisor.Stopper, "install", lambda self: None)

        stopper_holder = {}
        real_init = supervisor.Stopper.__init__

        def capture(self):
            real_init(self)
            stopper_holder["it"] = self

        monkeypatch.setattr(supervisor.Stopper, "__init__", capture)

        def run_then_ask_to_stop(conn, navigator, settings, **kwargs):
            harness["runs"].append(1)
            stopper_holder["it"].requested = True
            stopper_holder["it"].signal_name = "SIGTERM"
            return supervisor.runner.RunReport(attempted=1, success=1)

        monkeypatch.setattr(supervisor.runner, "run", run_then_ask_to_stop)
        report = supervisor.supervise(conn, settings, sleep=sleeper(harness))

        assert report.passes == 1
        assert "SIGTERM" in report.stopped_because
        assert original is not None
