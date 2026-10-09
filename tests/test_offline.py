"""Losing this machine's own network, as opposed to the portal turning us away.

Before this, an outage of more than about five minutes marked up to eight parts
failed for the whole cycle and ended in a consecutive_failures halt, which the
runbook reads as possible blocking, while the alert about it could not leave the
machine. Now the part goes back in the queue, the browser closes, and the run
carries on by itself when the network returns.
"""

from __future__ import annotations

import pytest

from txtav import db, guard, runner, supervisor

from test_runner import FakeNavigator as PartNavigator
from test_runner import fixture_html, seed
from test_runner import conn as runner_conn, settings as runner_settings  # noqa: F401
from test_supervisor import FakeNavigator as SessionNavigator, sleeper
from test_supervisor import conn, harness, settings  # noqa: F401


OFFLINE_ERROR = RuntimeError(
    "Page.goto: net::ERR_INTERNET_DISCONNECTED at https://ww2.txtav.com/Parts/x"
)


class TestTellingOfflineFromRefused:
    @pytest.mark.parametrize(
        "message",
        [
            "Page.goto: net::ERR_INTERNET_DISCONNECTED at https://ww2.txtav.com/",
            "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://ww2.txtav.com/",
            "Page.goto: net::ERR_NETWORK_CHANGED at https://ww2.txtav.com/",
        ],
    )
    def test_no_network_on_this_machine_is_offline(self, message):
        decision = guard.assess_transport(RuntimeError(message))
        assert decision.kind == "offline"
        assert not decision.should_stop

    def test_a_reset_connection_is_not_assumed_innocent(self):
        # A firewall dropping us looks exactly like this, so it stays with the
        # tripwire rather than being waited out.
        decision = guard.assess_transport(
            RuntimeError("Page.goto: net::ERR_CONNECTION_RESET at https://ww2.txtav.com/")
        )
        assert decision.kind == "transport"


class TestThePassStepsAsideWhenOffline:
    def test_the_part_is_requeued_not_failed(self, runner_conn, runner_settings):
        pns = ["6622168-22", "260-1014-33"]
        seed(runner_conn, pns)
        nav = PartNavigator(
            {pn: fixture_html(pn) for pn in pns}, raises={"6622168-22": OFFLINE_ERROR}
        )

        report = runner.run(runner_conn, nav, runner_settings)

        assert report.offline is True
        assert report.failed == 0
        assert report.halted is None
        assert db.open_halt(runner_conn) is None
        assert nav.requested == ["6622168-22"], "no retries spent, and no next part tried"
        status = runner_conn.execute(
            "SELECT status FROM jobs WHERE pn = '6622168-22'"
        ).fetchone()["status"]
        assert status == "pending"

    def test_it_does_not_count_towards_the_tripwire(self, runner_conn, runner_settings):
        seed(runner_conn, ["6622168-22"])
        tripwire = guard.ConsecutiveFailureTripwire(1)
        nav = PartNavigator({"6622168-22": ""}, raises={"6622168-22": OFFLINE_ERROR})

        runner.run(runner_conn, nav, runner_settings, tripwire=tripwire)

        assert tripwire.count == 0


class TestTheSupervisorWaitsForTheNetwork:
    def test_it_waits_then_carries_on_and_says_so(self, conn, settings, harness):
        harness["results"] = [supervisor.runner.RunReport(attempted=1, offline=True)]
        report = supervisor.supervise(conn, settings, max_passes=2, sleep=sleeper(harness))

        assert report.passes == 2, "it carried on by itself"
        assert supervisor.IDLE_SLEEP_SECONDS in harness["slept"]
        assert "TXT run resumed after a network outage" in harness["alerts"]
        assert db.open_halt(conn) is None

    def test_offline_at_the_session_check_is_not_a_logout(self, conn, settings, harness):
        offline = guard.Decision(guard.Verdict.RETRY, "offline", "no network")
        harness["navigator"] = SessionNavigator(answers=[offline, offline, guard.OK])
        report = supervisor.supervise(conn, settings, max_passes=1, sleep=sleeper(harness))

        assert report.passes == 1
        assert db.open_halt(conn) is None
        assert "TXT session expired" not in harness["alerts"]

    def test_it_gives_up_eventually(self, conn, settings, harness, monkeypatch):
        monkeypatch.setattr(supervisor, "MAX_OFFLINE_HOURS", 0)
        offline = guard.Decision(guard.Verdict.RETRY, "offline", "no network")
        harness["navigator"] = SessionNavigator(answers=[offline])
        report = supervisor.supervise(conn, settings, sleep=sleeper(harness))

        assert report.stopped_because == "offline too long"
        assert db.open_halt(conn) is None
