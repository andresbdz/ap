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

from txtav import alerts, db, logging_setup, runner
from txtav.config import Settings
from txtav.navigator import Navigator, browser_session
from txtav.pacer import in_window

log = logging_setup.get_logger("txtav.supervisor")

# How long to wait when there is nothing to do: the cycle is complete and the
# next one is not due yet, or we are outside the operating window.
IDLE_SLEEP_SECONDS = 300

# How long to wait before re-reading the queue after a normal pass. Short,
# because the pacer already spaces the requests themselves.
PASS_SLEEP_SECONDS = 5


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
    sleep=time.sleep,
    now=None,
) -> SupervisorReport:
    """Keep running passes until stopped, halted, or a limit is reached."""
    from datetime import datetime

    clock = now or datetime.now
    report = SupervisorReport()
    stopper = Stopper()
    stopper.install()

    with browser_session(settings.profile_dir, settings.page_timeout_seconds) as page:
        navigator = Navigator(page, settings.base_url)

        while True:
            if stopper.requested:
                report.stopped_because = f"asked to stop ({stopper.signal_name})"
                break
            if max_passes is not None and report.passes >= max_passes:
                report.stopped_because = "reached the pass limit"
                break
            if max_hours is not None and report.elapsed_hours >= max_hours:
                report.stopped_because = "reached the time limit"
                break

            halt = db.open_halt(conn)
            if halt is not None:
                report.stopped_because = f"halted: {halt['kind']}"
                log.critical(
                    "supervisor_halted", kind=halt["kind"], detail=halt["detail"]
                )
                alerts.send(
                    settings.alert_webhook,
                    f"TXT run halted: {halt['kind']}",
                    f"{halt['detail']}\n\nNothing further will run until this is "
                    f"cleared by hand.",
                )
                break

            if not in_window(clock(), settings.window_start, settings.window_end):
                log.info("outside_operating_window", sleeping=IDLE_SLEEP_SECONDS)
                sleep(IDLE_SLEEP_SECONDS)
                continue

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
                break

            result = runner.run(conn, navigator, settings)
            report.passes += 1
            report.parts += result.attempted

            if result.halted is not None:
                report.stopped_because = f"halted: {result.halted.kind}"
                break

            if result.attempted == 0:
                # Either the cycle is complete and the next is not due, or the
                # queue is momentarily empty. Either way there is nothing to be
                # gained from asking again immediately.
                log.info("nothing_to_do", sleeping=IDLE_SLEEP_SECONDS)
                sleep(IDLE_SLEEP_SECONDS)
            else:
                sleep(PASS_SLEEP_SECONDS)

    log.info(
        "supervisor_finished",
        passes=report.passes,
        parts=report.parts,
        reason=report.stopped_because,
    )
    return report
