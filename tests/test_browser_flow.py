"""Pacing, backoff and waiting: how hard the portal is asked, and when we wait.

Each test pins one rule about load on the portal or about telling a slow page
from a refusal. None of them is about appearing to be anything other than what
this is.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone

import pytest

from txtav import db, guard, instance_lock, pacer, runner
from txtav.config import Settings

from test_runner import FakeNavigator, _Fetch, conn, seed, settings  # noqa: F401


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _pacer(**kw) -> pacer.Pacer:
    clock = FakeClock()
    return pacer.Pacer(11.0, 0.0, sleep=clock.sleep, monotonic=clock.monotonic, **kw)


class TestThePaceOnlyEverSlowsDown:
    def test_a_steady_portal_keeps_the_agreed_interval(self):
        p = _pacer(slowdown_threshold=2.0, max_interval=60.0)
        for _ in range(200):
            p.observe(3.0)
        assert p.current_interval == 11.0

    def test_a_fast_portal_is_not_an_invitation_to_go_faster(self):
        p = _pacer(slowdown_threshold=2.0, max_interval=60.0)
        for _ in range(100):
            p.observe(3.0)
        for _ in range(100):
            p.observe(0.2)
        assert p.current_interval == 11.0

    def test_one_slow_page_in_twenty_does_not_trip_it(self):
        # The pilot's own shape: 5.3 % of gaps ran past 13 s.
        p = _pacer(slowdown_threshold=2.0, max_interval=60.0)
        for i in range(400):
            p.observe(35.0 if i % 20 == 19 else 3.0)
            assert p.current_interval == 11.0, f"stretched at page {i}"

    def test_a_sustained_slowdown_stretches_the_interval_in_proportion(self):
        p = _pacer(slowdown_threshold=2.0, max_interval=60.0)
        for _ in range(200):
            p.observe(3.0)
        for _ in range(30):
            p.observe(9.0)  # the portal has become three times slower
        assert p.slowdown > 2.0
        assert 22.0 < p.current_interval <= 33.0

    def test_the_stretch_is_capped(self):
        p = _pacer(slowdown_threshold=2.0, max_interval=60.0)
        for _ in range(200):
            p.observe(1.0)
        for _ in range(60):
            p.observe(40.0)
        assert p.current_interval == 60.0

    def test_nothing_stretches_before_a_baseline_exists(self):
        p = _pacer(slowdown_threshold=2.0, max_interval=60.0)
        p.observe(1.0)
        for _ in range(pacer.Pacer.WARMUP - 2):
            p.observe(30.0)
        assert p.current_interval == 11.0

    def test_off_means_off(self):
        p = _pacer(slowdown_threshold=0.0, max_interval=60.0)
        for _ in range(100):
            p.observe(1.0)
        for _ in range(100):
            p.observe(40.0)
        assert p.current_interval == 11.0


class TestRetryAfter:
    def test_seconds(self):
        assert guard.retry_after_seconds("120") == 120.0

    def test_an_http_date(self):
        now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        assert guard.retry_after_seconds("Wed, 07 Oct 2026 12:02:00 GMT", now) == 120.0

    def test_a_date_in_the_past_means_now(self):
        now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        assert guard.retry_after_seconds("Wed, 07 Oct 2026 11:00:00 GMT", now) == 0.0

    @pytest.mark.parametrize("value", [None, "", "soon", "-5"])
    def test_absent_or_unreadable_is_none(self, value):
        assert guard.retry_after_seconds(value) is None


class TestBackoff:
    def test_full_jitter_stays_between_zero_and_the_ceiling(self):
        s = Settings(backoff_base_seconds=5.0, backoff_cap_seconds=60.0)
        for attempt, ceiling in ((1, 5.0), (2, 10.0), (3, 20.0), (6, 60.0)):
            for _ in range(200):
                assert 0.0 <= runner._backoff(s, attempt) <= ceiling


class _ScriptedNavigator:
    """Returns a scripted sequence of fetches for one part."""

    def __init__(self, fetches):
        self.fetches = list(fetches)
        self.requested = 0

    def fetch(self, pn):
        self.requested += 1
        return self.fetches.pop(0)


def _retry(kind="server_error", retry_after=None):
    return _Fetch(
        "A-1",
        "",
        guard.Decision(guard.Verdict.RETRY, kind, f"{kind} for A-1"),
        retry_after=retry_after,
    )


class TestRetriesWaitAndRespectTheServer:
    def _settings(self, **kw) -> Settings:
        base = dict(
            request_interval_seconds=2.0,
            jitter_seconds=0.0,
            backoff_base_seconds=0.0,
            max_network_retries=3,
        )
        base.update(kw)
        return Settings(**base)

    def test_a_retry_after_within_the_cap_is_honoured(self, monkeypatch):
        slept = []
        monkeypatch.setattr(runner.time, "sleep", slept.append)
        nav = _ScriptedNavigator([_retry(retry_after="30")] * 3)
        attempt = runner._attempt(
            nav, self._settings(), "A-1", _pacer(), runner.RunReport()
        )
        assert attempt.record is None and attempt.decision is None
        assert slept == [30.0, 30.0], "waited as asked, and not after the last try"

    def test_a_retry_after_beyond_the_cap_stops_for_a_person(self, monkeypatch):
        monkeypatch.setattr(runner.time, "sleep", lambda s: None)
        nav = _ScriptedNavigator([_retry(retry_after="3600")])
        attempt = runner._attempt(
            nav, self._settings(), "A-1", _pacer(), runner.RunReport()
        )
        assert attempt.decision is not None
        assert attempt.decision.kind == "service_unavailable"
        assert attempt.decision.should_stop
        assert nav.requested == 1, "no second request after being told to stay away"

    def test_a_page_that_never_settled_is_retried_not_halted(self, monkeypatch):
        # It used to reach the extractor half rendered, find no part heading
        # and raise a halt only a person could clear.
        monkeypatch.setattr(runner.time, "sleep", lambda s: None)
        nav = _ScriptedNavigator([_retry("page_not_settled")] * 3)
        attempt = runner._attempt(
            nav, self._settings(), "A-1", _pacer(), runner.RunReport()
        )
        assert attempt.decision is None, "no halt"
        assert attempt.record is None
        assert nav.requested == 3

    def test_every_page_time_reaches_the_pacer(self, monkeypatch):
        monkeypatch.setattr(runner.time, "sleep", lambda s: None)
        seen = []

        class Watching(pacer.Pacer):
            def observe(self, seconds):
                seen.append(seconds)

        fetches = [_retry(), _retry()]
        for f, seconds in zip(fetches, (4.0, 7.0)):
            f.seconds = seconds
        nav = _ScriptedNavigator(fetches + [_retry()])
        runner._attempt(
            nav, self._settings(), "A-1", Watching(0.0), runner.RunReport()
        )
        assert seen[:2] == [4.0, 7.0]


class TestADryRunDoesNotConsumeTheCycle:
    """Marking parts done without storing anything lost their prices for the cycle."""

    def test_its_parts_stay_pending(self, conn, settings):
        from test_runner import fixture_html

        pns = ["6622168-22", "260-1014-33"]
        seed(conn, pns)
        nav = FakeNavigator({pn: fixture_html(pn) for pn in pns})

        report = runner.run(conn, nav, settings, dry_run=True)

        assert report.success == 2
        statuses = {r["pn"]: r["status"] for r in conn.execute("SELECT pn, status FROM jobs")}
        assert statuses == {pn: "pending" for pn in pns}


class TestTheDriverDyingIsNotANetworkFault:
    def test_it_is_filed_as_browser_gone(self):
        # Seen in the log on 2026-10-06, then retried as a network error.
        exc = RuntimeError("Page.goto: Connection closed while reading from the driver")
        assert guard.assess_transport(exc).kind == "browser_gone"


class TestOneProcessAtATime:
    def test_a_second_holder_is_refused(self, tmp_path):
        path = tmp_path / "txtav.lock"
        with instance_lock.InstanceLock(path):
            with pytest.raises(instance_lock.AlreadyRunning):
                instance_lock.InstanceLock(path).acquire()

    def test_it_is_free_again_once_released(self, tmp_path):
        path = tmp_path / "txtav.lock"
        with instance_lock.InstanceLock(path):
            pass
        with instance_lock.InstanceLock(path):
            pass

    def test_another_process_is_refused_while_it_is_held(self, tmp_path):
        path = tmp_path / "txtav.lock"
        probe = (
            "import sys\n"
            "from txtav import instance_lock\n"
            "try:\n"
            f"    instance_lock.InstanceLock({str(path)!r}).acquire()\n"
            "    print('acquired')\n"
            "except instance_lock.AlreadyRunning:\n"
            "    print('refused')\n"
        )
        with instance_lock.InstanceLock(path):
            held = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
        free = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)

        assert held.stdout.strip() == "refused"
        assert free.stdout.strip() == "acquired", "released when the holder let go"
