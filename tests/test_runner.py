"""Run loop tests, driven by a fake navigator so no browser is involved.

The behaviour under test is mostly about stopping: when the loop must halt,
what it must not write, and that an interrupted run resumes where it left off.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from txtav import db, extract, guard, runner
from txtav.config import Settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "6622168-22.html").exists(),
    reason="fixtures not captured; run scripts/capture_fixtures.py",
)


def fixture_html(pn: str) -> str:
    return (FIXTURES / f"{pn}.html").read_text(encoding="utf-8")


class FakeNavigator:
    """Serves saved pages, or whatever a test tells it to serve."""

    def __init__(self, pages: dict[str, str], *, raises: dict[str, Exception] | None = None):
        self.pages = pages
        self.raises = raises or {}
        self.requested: list[str] = []

    def fetch(self, pn: str):
        self.requested.append(pn)
        if pn in self.raises:
            raise self.raises[pn]
        html = self.pages[pn]
        # Mirror the real Navigator, which shows the guard page.inner_text(),
        # never the markup. Raw HTML would trip the captcha rule on every page.
        text = extract.visible_text(html)
        decision = guard.assess(200, f"https://ww2.txtav.com/x/{pn}", text)
        return _Fetch(pn, html, decision)


class _Fetch:
    def __init__(self, pn, html, decision, *, seconds=1.0, retry_after=None):
        self.pn, self.html, self.decision = pn, html, decision
        self.url = self.final_url = f"https://ww2.txtav.com/x/{pn}"
        self.status = 200
        self.text = html
        self.settled = True
        self.seconds = seconds
        self.retry_after = retry_after


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=tmp_path / "t.db",
        request_interval_seconds=2.0,
        jitter_seconds=0.0,
        backoff_base_seconds=0.0,
        max_network_retries=2,
        consecutive_failure_halt=3,
    )


@pytest.fixture
def conn(settings, monkeypatch):
    # The pacer would otherwise make every test take 11 seconds per part.
    monkeypatch.setattr(runner.Pacer, "wait", lambda self: 0.0)
    connection = db.connect(settings.db_path)
    yield connection
    connection.close()


def seed(conn, pns: list[str]) -> None:
    with db.transaction(conn):
        conn.executemany(
            "INSERT INTO parts(pn, dedupe_key, wh, source_row, output_row)"
            " VALUES (?,?,'TXT',?,?)",
            [(pn, pn.upper(), i + 200, i + 1) for i, pn in enumerate(pns, start=1)],
        )


class TestHappyPath:
    def test_processes_every_pending_part(self, conn, settings):
        pns = ["6622168-22", "260-1014-33", "9912125-2EX"]
        seed(conn, pns)
        nav = FakeNavigator({pn: fixture_html(pn) for pn in pns})

        report = runner.run(conn, nav, settings)

        assert report.attempted == 3
        assert report.success == 3
        assert report.halted is None
        assert nav.requested == pns

    def test_writes_the_derived_values(self, conn, settings):
        seed(conn, ["9912125-2EX"])
        nav = FakeNavigator({"9912125-2EX": fixture_html("9912125-2EX")})
        runner.run(conn, nav, settings)

        row = conn.execute("SELECT * FROM results WHERE pn = '9912125-2EX'").fetchone()
        assert row["sale_price"] == "7724.69"
        assert row["core_value"] == "16500.00"
        assert row["outright_price"] == "24224.69"
        assert row["condition_code"] == "OH"
        assert row["part_type"] == "OEM Exchange"
        assert row["alt_pn"] == "9912125-2"
        assert row["quantity"] == 46

    def test_a_missing_part_is_recorded_as_not_found(self, conn, settings):
        seed(conn, ["ZZZZ-9999"])
        nav = FakeNavigator({"ZZZZ-9999": fixture_html("ZZZZ-9999")})
        report = runner.run(conn, nav, settings)

        assert report.not_found == 1
        assert report.failed == 0
        status = conn.execute("SELECT status FROM jobs WHERE pn = 'ZZZZ-9999'").fetchone()
        assert status["status"] == "not_found"

    def test_dry_run_reports_without_writing_results(self, conn, settings):
        seed(conn, ["6622168-22"])
        nav = FakeNavigator({"6622168-22": fixture_html("6622168-22")})
        report = runner.run(conn, nav, settings, dry_run=True)

        assert report.success == 1
        assert conn.execute("SELECT COUNT(*) c FROM results").fetchone()["c"] == 0

    def test_limit_stops_early(self, conn, settings):
        pns = ["6622168-22", "260-1014-33", "9912125-2EX"]
        seed(conn, pns)
        nav = FakeNavigator({pn: fixture_html(pn) for pn in pns})
        report = runner.run(conn, nav, settings, limit=2)

        assert report.attempted == 2


class TestStopping:
    def test_a_refusal_halts_and_leaves_the_part_pending(self, conn, settings):
        pns = ["6622168-22", "260-1014-33"]
        seed(conn, pns)
        nav = FakeNavigator(
            {
                "6622168-22": fixture_html("6622168-22"),
                "260-1014-33": "<html><body>Access Denied</body></html>",
            }
        )
        report = runner.run(conn, nav, settings)

        assert report.halted is not None
        assert report.halted.kind == "access_denied"
        # The part must stay pending so it is retried after a human clears it.
        row = conn.execute("SELECT status FROM jobs WHERE pn = '260-1014-33'").fetchone()
        assert row["status"] == "pending"

    def test_a_halt_blocks_the_next_run_entirely(self, conn, settings):
        seed(conn, ["6622168-22"])
        db.raise_halt(conn, "rate_limited", "HTTP 429")
        nav = FakeNavigator({"6622168-22": fixture_html("6622168-22")})

        report = runner.run(conn, nav, settings)

        assert report.attempted == 0
        assert nav.requested == []

    def test_an_expired_session_stops_without_writing_blanks(self, conn, settings):
        seed(conn, ["6622168-22"])
        nav = FakeNavigator(
            {"6622168-22": "<html><body>Hello, Log In! My Account Log In Register</body></html>"}
        )
        report = runner.run(conn, nav, settings)

        assert report.halted.kind == "session_expired"
        assert conn.execute("SELECT COUNT(*) c FROM results").fetchone()["c"] == 0

    def test_losing_pricing_access_stops_the_run(self, conn, settings):
        seed(conn, ["6622168-22"])
        nav = FakeNavigator(
            {"6622168-22": "<html><body>Request Access to View Pricing and Availability</body></html>"}
        )
        assert runner.run(conn, nav, settings).halted.kind == "pricing_revoked"

    def test_repeated_failures_trip_the_tripwire(self, conn, settings):
        # A network fault rather than a timeout: the page is reachable enough
        # to fail, so this is the tripwire's case, not a hung browser's.
        pns = ["a", "b", "c", "d"]
        seed(conn, pns)
        nav = FakeNavigator(
            {pn: "" for pn in pns},
            raises={pn: RuntimeError("net::ERR_CONNECTION_RESET") for pn in pns},
        )
        report = runner.run(conn, nav, settings)

        assert report.halted is not None
        assert report.halted.kind == "consecutive_failures"
        assert report.failed == 3

    def test_parts_that_only_time_out_end_the_pass_without_a_halt(self, conn, settings):
        # The shape of a hung browser. Halting would send a person looking for a
        # block; replacing the browser is the proportionate response, and the
        # supervisor's tripwire still counts across it.
        pns = ["a", "b", "c", "d"]
        seed(conn, pns)
        nav = FakeNavigator(
            {pn: "" for pn in pns},
            raises={pn: TimeoutError("Timeout 45000ms exceeded") for pn in pns},
        )
        report = runner.run(conn, nav, settings)

        assert report.halted is None
        assert report.browser_suspect is True
        assert report.failed == runner.BROWSER_SUSPECT_AFTER
        assert db.open_halt(conn) is None
        # Back in the queue: failed is final for the cycle, and these parts did
        # nothing wrong.
        statuses = {r["pn"]: r["status"] for r in conn.execute("SELECT pn, status FROM jobs")}
        assert statuses == {pn: "pending" for pn in pns}

    def test_an_unrecognised_page_halts_rather_than_being_skipped(self, conn, settings):
        seed(conn, ["6622168-22"])
        nav = FakeNavigator({"6622168-22": "<html><body>Scheduled maintenance</body></html>"})
        assert runner.run(conn, nav, settings).halted.kind == "unrecognised_page"


class TestPacingCoversRetries:
    """The pacing is a promise about load, so a retry has to wait like any
    other request.

    Regression: only first attempts were gated. With roughly 60% of requests
    returning HTTP 500 and recovering on retry, that made the real rate one
    request every 6.9 seconds instead of the 11 seconds promised.
    """

    def test_every_attempt_waits(self, conn, settings, monkeypatch):
        waits: list[int] = []
        monkeypatch.setattr(runner.Pacer, "wait", lambda self: waits.append(1) or 0.0)

        seed(conn, ["a", "b"])
        # Each part fails twice and is never served, so each one makes
        # max_network_retries attempts.
        nav = FakeNavigator(
            {"a": "", "b": ""},
            raises={pn: TimeoutError("Timeout 45000ms exceeded") for pn in ("a", "b")},
        )
        settings.consecutive_failure_halt = 99
        runner.run(conn, nav, settings)

        assert len(nav.requested) == 2 * settings.max_network_retries
        assert len(waits) == len(nav.requested)

    def test_a_clean_run_waits_once_per_part(self, conn, settings, monkeypatch):
        waits: list[int] = []
        monkeypatch.setattr(runner.Pacer, "wait", lambda self: waits.append(1) or 0.0)

        pns = ["6622168-22", "260-1014-33"]
        seed(conn, pns)
        runner.run(conn, FakeNavigator({pn: fixture_html(pn) for pn in pns}), settings)

        assert len(waits) == 2


class TestAlerting:
    def test_a_halt_sends_an_alert(self, conn, settings, monkeypatch):
        sent: list[tuple[str, str]] = []
        monkeypatch.setattr(
            runner.alerts, "send", lambda hook, title, body: sent.append((title, body))
        )
        seed(conn, ["6622168-22"])
        nav = FakeNavigator({"6622168-22": "<html><body>Access Denied</body></html>"})
        runner.run(conn, nav, settings)

        assert sent and "halted" in sent[0][0]

    def test_a_healthy_run_sends_nothing(self, conn, settings, monkeypatch):
        sent: list[tuple[str, str]] = []
        monkeypatch.setattr(
            runner.alerts, "send", lambda hook, title, body: sent.append((title, body))
        )
        pns = ["6622168-22", "260-1014-33", "9912125-2EX"]
        seed(conn, pns)
        runner.run(conn, FakeNavigator({pn: fixture_html(pn) for pn in pns}), settings)

        assert sent == []

    def test_a_suspiciously_empty_run_is_alerted_and_logged(
        self, conn, settings, monkeypatch
    ):
        # Nothing errors here: every part simply fails to load. In the database
        # that is indistinguishable from a quiet day, which is the danger.
        sent: list[tuple[str, str]] = []
        monkeypatch.setattr(
            runner.alerts, "send", lambda hook, title, body: sent.append((title, body))
        )
        settings.consecutive_failure_halt = 99
        pns = ["a", "b", "c", "d"]
        seed(conn, pns)
        nav = FakeNavigator(
            {pn: "" for pn in pns},
            raises={pn: TimeoutError("Timeout 45000ms exceeded") for pn in pns},
        )
        runner.run(conn, nav, settings)

        assert sent and "little data" in sent[0][0]
        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM events")}
        assert "suspiciously_empty_run" in kinds


class TestResume:
    def test_a_second_run_only_picks_up_what_is_left(self, conn, settings):
        pns = ["6622168-22", "260-1014-33", "9912125-2EX"]
        seed(conn, pns)
        pages = {pn: fixture_html(pn) for pn in pns}

        runner.run(conn, FakeNavigator(pages), settings, limit=1)
        second = FakeNavigator(pages)
        report = runner.run(conn, second, settings)

        assert report.attempted == 2
        assert "6622168-22" not in second.requested

    def test_a_part_abandoned_in_flight_is_requeued(self, conn, settings):
        seed(conn, ["6622168-22"])
        cycle_id = runner.current_cycle(conn, settings.cycle_days)
        conn.execute("UPDATE jobs SET status = 'in_flight' WHERE pn = '6622168-22'")

        assert runner.reset_stale(conn, cycle_id) == 1
        assert runner.pending(conn, cycle_id, None) == [("6622168-22", 2)]
