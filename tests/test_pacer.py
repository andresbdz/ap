"""Pacing and cycle planning tests."""

from __future__ import annotations

from datetime import datetime, time as tod

import pytest

from txtav import pacer

REAL_PART_COUNT = 109_589


class TestCyclePlan:
    def test_the_real_workload(self):
        plan = pacer.plan_cycle(REAL_PART_COUNT, 14)
        assert round(plan.per_day) == 7828
        assert round(plan.per_minute, 2) == 5.44
        assert round(plan.required_interval, 1) == 11.0

    def test_a_shorter_cycle_demands_a_faster_pace(self):
        # The Drive copy of the scope says "once a week". That halves the
        # interval, which doubles the load on the portal.
        weekly = pacer.plan_cycle(REAL_PART_COUNT, 7)
        assert round(weekly.required_interval, 1) == 5.5

    def test_a_daytime_window_demands_a_faster_pace(self):
        windowed = pacer.plan_cycle(REAL_PART_COUNT, 14, hours_per_day=12)
        assert round(windowed.required_interval, 1) == 5.5

    def test_an_empty_list_does_not_divide_by_zero(self):
        assert pacer.plan_cycle(0, 14).required_interval == 0.0

    def test_describe_mentions_the_interval(self):
        assert "11.0s" in pacer.plan_cycle(REAL_PART_COUNT, 14).describe()


class TestWindow:
    def test_no_window_is_a_full_day(self):
        assert pacer.window_hours(None, None) == 24.0

    def test_daytime_window(self):
        assert pacer.window_hours(tod(8, 0), tod(20, 0)) == 12.0

    def test_window_wrapping_midnight(self):
        assert pacer.window_hours(tod(22, 0), tod(6, 0)) == 8.0

    @pytest.mark.parametrize(
        "hour,expected", [(7, False), (8, True), (19, True), (20, False)]
    )
    def test_in_daytime_window(self, hour, expected):
        now = datetime(2026, 10, 2, hour, 30)
        assert pacer.in_window(now, tod(8, 0), tod(20, 0)) is expected

    @pytest.mark.parametrize(
        "hour,expected", [(23, True), (3, True), (7, False), (21, False)]
    )
    def test_in_window_wrapping_midnight(self, hour, expected):
        now = datetime(2026, 10, 2, hour, 0)
        assert pacer.in_window(now, tod(22, 0), tod(6, 0)) is expected

    def test_without_a_window_every_time_is_allowed(self):
        assert pacer.in_window(datetime(2026, 10, 2, 3, 0), None, None)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class TestPacer:
    def test_first_request_does_not_wait(self):
        clock = FakeClock()
        p = pacer.Pacer(11.0, 0.0, sleep=clock.sleep, monotonic=clock.monotonic)
        assert p.wait() == 0.0
        assert clock.slept == []

    def test_waits_the_remainder_of_the_interval(self):
        clock = FakeClock()
        p = pacer.Pacer(11.0, 0.0, sleep=clock.sleep, monotonic=clock.monotonic)
        p.wait()
        clock.now += 4.0  # the page took 4 seconds
        assert p.wait() == pytest.approx(7.0)

    def test_a_slow_page_does_not_push_the_schedule_back(self):
        # Measuring start to start means a page that took longer than the
        # interval costs nothing extra.
        clock = FakeClock()
        p = pacer.Pacer(11.0, 0.0, sleep=clock.sleep, monotonic=clock.monotonic)
        p.wait()
        clock.now += 25.0
        assert p.wait() == 0.0

    def test_jitter_stays_inside_its_bounds(self):
        clock = FakeClock()
        p = pacer.Pacer(11.0, 2.0, sleep=clock.sleep, monotonic=clock.monotonic)
        p.wait()
        for _ in range(50):
            waited = p.wait()
            assert 9.0 - 1e-9 <= waited <= 13.0 + 1e-9

    def test_negative_interval_is_rejected(self):
        with pytest.raises(ValueError):
            pacer.Pacer(-1.0)
