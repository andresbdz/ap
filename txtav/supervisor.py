"""The long running process: keeps the cycle moving, day after day.

`run` processes whatever is pending and returns. That is the right shape for a
command, but a 14 day cycle needs something that keeps calling it, survives the
ordinary interruptions, and knows the difference between "nothing to do just
now" and "stop and fetch a human".

What it does not do is retry past a halt. A 403, a CAPTCHA or a lost session
ends the loop and waits for a person, because continuing is exactly the failure
this project cannot afford. The supervisor is a scheduler, not a way around the
guard.
"""

from __future__ import annotations

import signal
import sqlite3
import time
from dataclasses import dataclass, field

from txtav import alerts, db, guard, logging_setup, runner
from txtav.config import Settings
from txtav.navigator import Navigator, browser_session
from txtav.pacer import Pacer, in_window

log = logging_setup.get_logger("txtav.supervisor")

# How long to wait when there is nothing to do: the cycle is complete and the
# next one is not due yet, or we are outside the operating window.
IDLE_SLEEP_SECONDS = 300

# How long to wait before re-reading the queue after a normal pass. Short,
# because the pacer already spaces the requests themselves.
PASS_SLEEP_SECONDS = 5

# Parts per pass. The loop only regains control between passes, so an
# unbounded pass means the supervisor checks nothing until the whole cycle is
# done. Measured on 2026-10-06: a supervise started with --max-hours 1.5 was
# still going at 2.5 hours, because its single pass had taken the entire queue
# of 109,351 parts, and SIGINT and SIGTERM were recorded but never acted on for
# the same reason. 200 parts is a little under 40 minutes at 11 seconds each,
# so every limit, the halt state and the session are rechecked that often.
PASS_PARTS = 200

# Passes before the browser is replaced. One Chromium tab is asked to navigate
# 109,589 pages over 14 days, and nothing in a renderer is designed for that:
# memory grows and there is no way to give it back short of a new context. The
# profile directory holds the session, so closing and reopening costs a couple
# of seconds and loses nothing. At 200 parts a pass this is a fresh browser
# roughly every five hours.
#
# Stated as a precaution, not a fix: no leak has been measured here. It is
# cheap, and the failure it guards against is a process dying on day nine.
SESSION_PASSES = 8


@dataclass
class SupervisorReport:
    passes: int = 0
    parts: int = 0
    started_at: float = field(default_factory=time.time)
    stopped_because: str = ""

    @property
    def elapsed_hours(self) -> float:
        return (time.time() - self.started_at) / 3600

    def summary(self) -> str:
        return (
            f"passes:   {self.passes}\n"
            f"parts:    {self.parts:,}\n"
            f"ran for:  {self.elapsed_hours:.1f} h\n"
            f"stopped:  {self.stopped_because}"
        )


class Stopper:
    """Turns SIGINT and SIGTERM into a clean stop at the next pass boundary.

    Killing the process mid part is survivable, since the job is committed per
    part and a stale in_flight row is reset on the next start. Stopping between
    passes is tidier and makes a deliberate shutdown look different from a
    crash in the logs.
    """

    def __init__(self) -> None:
        self.requested = False
        self.signal_name = ""

    def install(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._handle)

    def _handle(self, signum, _frame) -> None:
        self.requested = True
        self.signal_name = signal.Signals(signum).name
        log.info("stop_requested", signal=self.signal_name)


def supervise(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    max_passes: int | None = None,
    max_hours: float | None = None,
    pass_parts: int = PASS_PARTS,
    session_passes: int = SESSION_PASSES,
    sleep=time.sleep,
    now=None,
) -> SupervisorReport:
    """Keep running passes until stopped, halted, or a limit is reached.

    The browser is opened per segment of `session_passes` passes rather than
    once for the whole fourteen days, and is closed while the loop is idle or
    outside its operating window. The pacer and the failure tripwire, on the
    other hand, live for the whole call: built per pass they forgot what came
    before, which let the first request of every pass skip its wait and reset
    the failure count every 200 parts.
    """
    from datetime import datetime

    clock = now or datetime.now
    report = SupervisorReport()
    stopper = Stopper()
    stopper.install()
    pace = Pacer(settings.request_interval_seconds, settings.jitter_seconds)
    tripwire = guard.ConsecutiveFailureTripwire(settings.consecutive_failure_halt)

    def stop_now() -> str:
        """Why the loop should end, or an empty string to carry on."""
        if stopper.requested:
            return f"asked to stop ({stopper.signal_name})"
        if max_passes is not None and report.passes >= max_passes:
            return "reached the pass limit"
        if max_hours is not None and report.elapsed_hours >= max_hours:
            return "reached the time limit"

        halt = db.open_halt(conn)
        if halt is not None:
            log.critical("supervisor_halted", kind=halt["kind"], detail=halt["detail"])
            alerts.send(
                settings.alert_webhook,
                f"TXT run halted: {halt['kind']}",
                f"{halt['detail']}\n\nNothing further will run until this is "
                f"cleared by hand.",
            )
            return f"halted: {halt['kind']}"
        return ""

    def segment() -> bool:
        """Up to `session_passes` passes on one browser. True when idle.

        Sets report.stopped_because when the loop has to end.
        """
        with browser_session(settings.profile_dir, settings.page_timeout_seconds) as page:
            navigator = Navigator(page, settings.base_url)

            for _ in range(max(1, session_passes)):
                report.stopped_because = stop_now()
                if report.stopped_because:
                    return False
                if not in_window(clock(), settings.window_start, settings.window_end):
                    return False  # the outer loop sleeps, with the browser closed

                if not navigator.is_signed_in():
                    report.stopped_because = "session expired"
                    log.critical("supervisor_session_expired")
                    alerts.send(
                        settings.alert_webhook,
                        "TXT session expired",
                        "The portal session has lapsed. Someone needs to run "
                        "`txtav login` and sign in by hand, including the 2FA code. "
                        "Nothing is lost; the run resumes where it stopped.",
                    )
                    return False

                result = runner.run(
                    conn,
                    navigator,
                    settings,
                    limit=pass_parts,
                    pace=pace,
                    tripwire=tripwire,
                )
                report.passes += 1
                report.parts += result.attempted

                if result.halted is not None:
                    report.stopped_because = f"halted: {result.halted.kind}"
                    return False

                if result.attempted == 0:
                    # Either the cycle is complete and the next is not due, or
                    # the queue is momentarily empty. Either way there is
                    # nothing to be gained from asking again immediately, and
                    # nothing to be gained from holding the browser while we
                    # wait.
                    return True
                sleep(PASS_SLEEP_SECONDS)
        return False

    while True:
        report.stopped_because = stop_now()
        if report.stopped_because:
            break

        if not in_window(clock(), settings.window_start, settings.window_end):
            # Checked before the browser is opened, so an overnight window does
            # not leave an idle browser holding a session open all day.
            log.info("outside_operating_window", sleeping=IDLE_SLEEP_SECONDS)
            sleep(IDLE_SLEEP_SECONDS)
            continue

        try:
            idle = segment()
        except Exception as exc:  # noqa: BLE001 - an unattended stop must be heard
            # Before the browser was recycled it was launched once, in front of
            # whoever typed the command, so a failure to start was seen. Now it
            # relaunches every few hours with nobody watching, and a launch that
            # fails (a Playwright update, a full disk, a profile left locked)
            # would end the process with a traceback in a terminal nobody is
            # looking at. Anything escaping a segment is caught here instead,
            # written down and sent, and the loop stops: an unexplained fault is
            # not something to retry around.
            detail = f"{type(exc).__name__}: {exc}"
            report.stopped_because = f"crashed: {type(exc).__name__}"
            log.critical("supervisor_crashed", error=detail)
            db.log_event(conn, "critical", "supervisor_crashed", detail)
            alerts.send(
                settings.alert_webhook,
                "TXT supervisor stopped unexpectedly",
                f"{detail}\n\nNo halt was raised; nothing suggests the portal "
                f"refused us. Check the machine, then start `txtav supervise` "
                f"again. The run resumes where it stopped.",
            )
            break

        if report.stopped_because:
            break
        if idle:
            log.info("nothing_to_do", sleeping=IDLE_SLEEP_SECONDS)
            sleep(IDLE_SLEEP_SECONDS)
        else:
            log.info("browser_recycled", passes=report.passes)

    log.info(
        "supervisor_finished",
        passes=report.passes,
        parts=report.parts,
        reason=report.stopped_because,
    )
    return report
