"""Request pacing and cycle planning.

109,589 parts across a 14 day cycle is one part every 11.0 seconds, running
continuously. There is no slack in that: an hour lost is about 327 parts that
cannot be made up inside the cycle.

The interval is derived from the part count and the cycle length rather than
hard coded, so changing the list size cannot silently raise the request rate.
The jitter smooths bursts; it is not a disguise.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, time as time_of_day

SECONDS_PER_DAY = 24 * 3600


@dataclass(frozen=True)
class CyclePlan:
    part_count: int
    cycle_days: int
    hours_per_day: float

    @property
    def per_day(self) -> float:
        return self.part_count / self.cycle_days

    @property
    def per_hour(self) -> float:
        return self.per_day / self.hours_per_day

    @property
    def per_minute(self) -> float:
        return self.per_hour / 60

    @property
    def required_interval(self) -> float:
        """Seconds between requests needed to finish the cycle on time."""
        if self.part_count <= 0:
            return 0.0
        return (self.cycle_days * self.hours_per_day * 3600) / self.part_count

    def describe(self) -> str:
        return (
            f"{self.part_count:,} parts / {self.cycle_days} days"
            f" at {self.hours_per_day:g}h per day"
            f" = {self.per_day:,.0f}/day"
            f" = {self.per_hour:,.1f}/hour"
            f" = {self.per_minute:.2f}/min"
            f" = one every {self.required_interval:.1f}s"
        )


def plan_cycle(part_count: int, cycle_days: int, hours_per_day: float = 24.0) -> CyclePlan:
    return CyclePlan(part_count, cycle_days, hours_per_day)


def window_hours(start: time_of_day | None, end: time_of_day | None) -> float:
    """Hours available per day. 24 when no window is configured.

    A window that wraps past midnight is supported, because an overnight run is
    a reasonable thing to ask for.
    """
    if start is None or end is None:
        return 24.0
    start_s = start.hour * 3600 + start.minute * 60 + start.second
    end_s = end.hour * 3600 + end.minute * 60 + end.second
    span = end_s - start_s
    if span <= 0:
        span += SECONDS_PER_DAY
    return span / 3600


def in_window(now: datetime, start: time_of_day | None, end: time_of_day | None) -> bool:
    if start is None or end is None:
        return True
    current = now.time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


class Pacer:
    """Spaces the starts of two requests by `interval` seconds on average.

    Measuring from start to start rather than sleeping a fixed amount after
    each one means a slow page does not push the whole schedule back.

    `interval` is a mean, not a floor, and the distinction is worth stating
    because the docstring here used to claim a floor. Jitter is spread either
    side of it, so at the agreed 11.0 s with 2.0 s of jitter the gaps fall
    between 9 s and 13 s. That is deliberate: the commitment is a request rate
    over the cycle, 109,589 parts in 14 days, and spreading the jitter
    symmetrically is what keeps the mean on 11.0 s and the cycle on 14 days.
    Adding jitter only upwards would make 11.0 s a true floor but move the mean
    to 12.0 s, which finishes the cycle on day 15 and misses the date the client
    was given. There is no slack in the schedule to pay for a floor with.
    """

    def __init__(
        self,
        interval: float,
        jitter: float = 0.0,
        *,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        if interval < 0:
            raise ValueError("interval must not be negative")
        self.interval = interval
        self.jitter = max(0.0, jitter)
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_start: float | None = None

    def _target_gap(self) -> float:
        if self.jitter == 0:
            return self.interval
        return self.interval + random.uniform(-self.jitter, self.jitter)

    def wait(self) -> float:
        """Block until the next request may start. Returns seconds slept."""
        now = self._monotonic()
        if self._last_start is None:
            self._last_start = now
            return 0.0
        elapsed = now - self._last_start
        remaining = max(0.0, self._target_gap() - elapsed)
        if remaining:
            self._sleep(remaining)
        self._last_start = self._monotonic()
        return remaining
