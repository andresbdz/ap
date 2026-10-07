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
from collections import deque
from dataclasses import dataclass
from statistics import median
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

    The interval also stretches when the portal slows down, and only then. 11 s
    is what the client's deadline needs, not a rate the portal has approved, and
    nothing it publishes says what it can take. What it does say, every request,
    is how long it took to answer.

    "Recent" is the median page time of the last nine pages; "normal" is a slow
    moving average of that median. When recent reaches `slowdown_threshold`
    times normal, the portal is struggling, and the interval is multiplied by
    the same ratio: three times slower than usual means three times further
    apart, capped at `max_interval`. It is judged against the portal's own
    normal rather than a fixed number of seconds because that normal has not
    been measured yet, and a fixed threshold set below it would slow the entire
    cycle for no reason. It never goes below `interval`: a fast portal is not an
    invitation to go faster than was agreed. Scrapy's AutoThrottle keeps the
    same rule, never dropping below its configured delay.

    A median because one slow page in twenty is ordinary here (5.3 % of gaps in
    the pilot ran past 13 s): a single outlier never moves the median of nine,
    while five slow pages out of nine move it all the way. And the normal level
    learns ten times more slowly while the portal is struggling, because a
    baseline that absorbed the slowdown as it happened would quietly switch the
    protection off at exactly the moment it was needed; a first version did,
    reading a sustained threefold slowdown as under twofold after thirty pages.
    A slowdown that lasts for hours is still accepted as the new normal in time.
    """

    # Pages in the recent window. Odd, so the median is one real page time.
    RECENT_WINDOW = 9
    # How fast the normal level follows the recent median: about a hundred
    # pages in ordinary conditions, about a thousand while slowed down.
    BASELINE_SMOOTHING = 0.01
    BASELINE_SMOOTHING_WHILE_SLOW = 0.001
    # Observations before the baseline is trusted. Until then nothing stretches.
    WARMUP = 20

    def __init__(
        self,
        interval: float,
        jitter: float = 0.0,
        *,
        slowdown_threshold: float = 0.0,
        max_interval: float | None = None,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        if interval < 0:
            raise ValueError("interval must not be negative")
        self.interval = interval
        self.jitter = max(0.0, jitter)
        self.slowdown_threshold = max(0.0, slowdown_threshold)
        self.max_interval = max(interval, max_interval if max_interval is not None else interval)
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_start: float | None = None
        self._window: deque[float] = deque(maxlen=self.RECENT_WINDOW)
        self._baseline: float | None = None
        self._observed = 0

    def observe(self, seconds: float) -> None:
        """Record how long the portal took to serve one page."""
        if seconds < 0:
            return
        self._observed += 1
        self._window.append(seconds)
        recent = median(self._window)
        if self._baseline is None:
            self._baseline = recent
            return
        struggling = (
            self.slowdown_threshold
            and self._baseline > 0
            and recent >= self.slowdown_threshold * self._baseline
        )
        rate = self.BASELINE_SMOOTHING_WHILE_SLOW if struggling else self.BASELINE_SMOOTHING
        self._baseline += rate * (recent - self._baseline)

    @property
    def slowdown(self) -> float:
        """Recent page time over normal page time. 1.0 means business as usual."""
        if self._observed < self.WARMUP or not self._baseline or self._baseline <= 0:
            return 1.0
        return median(self._window) / self._baseline

    @property
    def current_interval(self) -> float:
        """The spacing in force now: the agreed interval, or longer if the portal is slow."""
        ratio = self.slowdown
        if not self.slowdown_threshold or ratio < self.slowdown_threshold:
            return self.interval
        return min(self.max_interval, self.interval * ratio)

    def _target_gap(self) -> float:
        base = self.current_interval
        if self.jitter == 0:
            return base
        return base + random.uniform(-self.jitter, self.jitter)

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
