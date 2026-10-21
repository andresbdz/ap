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
from txtav.pacer import in_window

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

# How many times in a row the dashboard may fail to answer, IDLE_SLEEP_SECONDS
# apart, before the loop gives up and asks for a person. Six is half an hour:
# long enough to ride out a router restart, short enough not to lose a night.
MAX_UNANSWERED_CHECKS = 6

# What a segment of passes on one browser ended in.
STOP = "stop"                # report.stopped_because says why
IDLE = "idle"                # nothing to do yet; wait with the browser closed
RECYCLE = "recycle"          # carry on with a fresh browser
UNREACHABLE = "unreachable"  # the portal or browser did not answer; wait, then retry
OFFLINE = "offline"          # this machine has no network; wait for it, longer

# How long to keep waiting for this machine's own network before giving up.
MAX_OFFLINE_HOURS = 12


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
    pace = runner.build_pacer(settings)
    tripwire = guard.ConsecutiveFailureTripwire(settings.consecutive_failure_halt)
    unanswered = 0  # consecutive session checks the portal did not answer
    offline_since: float | None = None  # when this machine lost its network

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

    def network_back() -> None:
        """Announce the end of an outage the first time the portal answers again.

        Here rather than at the end of a segment, which can be hours later. The
        alert about the outage could not be sent while it lasted, so this one is
        the only word anyone gets of it.
        """
        nonlocal offline_since
        if offline_since is None:
            return
        minutes = (time.time() - offline_since) / 60
        offline_since = None
        detail = (
            f"this machine had no network for about {minutes:.0f} minutes. The "
            f"parts in flight went back in the queue and the run carried on by itself."
        )
        log.warning("network_restored", minutes=round(minutes))
        db.log_event(conn, "warning", "network_restored", detail)
        alerts.send(settings.alert_webhook, "TXT run resumed after a network outage", detail)

    def signed_out(session: guard.Decision) -> str:
        """The session check found nobody signed in."""
        if report.passes == 0:
            # Nothing has been asked of the portal by this process yet, so this
            # is someone starting without having signed in, not a logout.
            report.stopped_because = "not signed in"
            log.critical("supervisor_not_signed_in")
            alerts.send(
                settings.alert_webhook,
                "TXT supervisor needs a sign in",
                "No session on start. Run `txtav login`, sign in by hand with "
                "the 2FA code, then start `txtav supervise` again.",
            )
            return STOP
        # Signed out after working: the brief's "forced logout", to be stopped
        # and logged for manual review. A halt records it the same way the run
        # loop already did for a logout seen mid pass, and the list of them is
        # also how the session's real lifetime will finally get measured.
        db.raise_halt(conn, "session_expired", session.detail)
        report.stopped_because = "session expired"
        log.critical("supervisor_session_expired", detail=session.detail)
        halt = db.open_halt(conn)
        alerts.send(
            settings.alert_webhook,
            "TXT session expired",
            "The portal session has lapsed. Run `txtav login` and sign in by "
            "hand with the 2FA code, check nothing else looks wrong, then "
            f"`txtav halt-clear --id {halt['id'] if halt else 'N'} --by <name>` "
            "and start `txtav supervise` again. Nothing is lost; the run "
            "resumes where it stopped.",
        )
        return STOP

    def segment() -> str:
        """Up to `session_passes` passes on one browser, then say what happened.

        Sets report.stopped_because whenever the answer is STOP.
        """
        with browser_session(
            settings.profile_dir,
            settings.page_timeout_seconds,
            channel=settings.browser_channel,
        ) as page:
            navigator = Navigator(page, settings.base_url)

            for _ in range(max(1, session_passes)):
                report.stopped_because = stop_now()
                if report.stopped_because:
                    return STOP
                if not in_window(clock(), settings.window_start, settings.window_end):
                    return RECYCLE  # the outer loop sleeps, with the browser closed

                session = navigator.check_session()
                if session.kind == "offline":
                    log.warning("offline_at_session_check", detail=session.detail)
                    return OFFLINE
                if session.verdict is guard.Verdict.RETRY or session.kind == "browser_gone":
                    # The portal or the browser did not answer. Not a reason to
                    # send someone to sign in: wait with the browser closed and
                    # look again, a bounded number of times.
                    log.warning("session_check_unanswered", kind=session.kind, detail=session.detail)
                    return UNREACHABLE
                if session.verdict is guard.Verdict.REAUTH:
                    return signed_out(session)
                if session.verdict is guard.Verdict.OK:
                    network_back()
                if session.should_stop:
                    # A CAPTCHA or a refusal on the dashboard is the same signal
                    # it would be on a part page.
                    db.raise_halt(conn, session.kind, session.detail)
                    report.stopped_because = f"halted: {session.kind}"
                    log.critical("supervisor_halted", kind=session.kind, detail=session.detail)
                    alerts.send(
                        settings.alert_webhook,
                        f"TXT run halted: {session.kind}",
                        f"{session.detail}\n\nSeen on the dashboard before any part "
                        f"was looked up. Nothing further will run until this is "
                        f"cleared by hand.",
                    )
                    return STOP

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
                    return STOP
                if result.offline:
                    return OFFLINE
                if result.browser_suspect:
                    # The page stopped answering at all. A fresh browser is the
                    # proportionate answer; the tripwire persists across it.
                    return RECYCLE

                if result.attempted == 0:
                    # Either the cycle is complete and the next is not due, or
                    # the queue is momentarily empty. Either way there is
                    # nothing to be gained from asking again immediately, and
                    # nothing to be gained from holding the browser while we
                    # wait.
                    return IDLE
                sleep(PASS_SLEEP_SECONDS)
        return RECYCLE

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
            outcome = segment()
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
            advice = (
                "Check the machine, then start `txtav supervise` again."
            )
            if any(marker in detail.lower() for marker in guard.PROFILE_IN_USE):
                # Measured: headed Chromium refuses a profile another Chromium
                # holds open. Usually a `txtav login` window left open, or a
                # browser orphaned by an earlier crash.
                advice = (
                    "The browser profile is open in another Chromium window, "
                    "often a `txtav login` left open or a browser left behind by "
                    "a crash. Close it, then start `txtav supervise` again."
                )
            report.stopped_because = f"crashed: {type(exc).__name__}"
            log.critical("supervisor_crashed", error=detail)
            db.log_event(conn, "critical", "supervisor_crashed", detail)
            alerts.send(
                settings.alert_webhook,
                "TXT supervisor stopped unexpectedly",
                f"{detail}\n\nNo halt was raised; nothing suggests the portal "
                f"refused us. {advice} The run resumes where it stopped.",
            )
            break

        if report.stopped_because:
            break
        if outcome == OFFLINE:
            # Waited out far longer than an unanswered portal, because this is
            # unambiguous: the machine itself has no network, nothing about the
            # portal is in question, and no alert can leave until it is back.
            # Twelve hours covers a power cut at the router overnight.
            if offline_since is None:
                offline_since = time.time()
                log.warning("waiting_for_network", sleeping=IDLE_SLEEP_SECONDS)
            hours = (time.time() - offline_since) / 3600
            if hours >= MAX_OFFLINE_HOURS:
                report.stopped_because = "offline too long"
                detail = (
                    f"this machine has had no network for {hours:.1f} hours. "
                    f"Start `txtav supervise` again once it is back."
                )
                log.critical("supervisor_offline_too_long", detail=detail)
                db.log_event(conn, "critical", "offline_too_long", detail)
                break
            sleep(IDLE_SLEEP_SECONDS)
            continue

        if outcome == UNREACHABLE:
            unanswered += 1
            if unanswered > MAX_UNANSWERED_CHECKS:
                report.stopped_because = "portal unreachable"
                detail = (
                    f"the dashboard did not answer {unanswered} times in a row, "
                    f"{IDLE_SLEEP_SECONDS // 60} minutes apart. No halt was raised; "
                    f"check the machine's connection, then start `txtav supervise` again."
                )
                log.critical("supervisor_portal_unreachable", detail=detail)
                db.log_event(conn, "critical", "portal_unreachable", detail)
                alerts.send(settings.alert_webhook, "TXT portal unreachable", detail)
                break
            log.info("waiting_for_portal", attempt=unanswered, sleeping=IDLE_SLEEP_SECONDS)
            sleep(IDLE_SLEEP_SECONDS)
            continue

        unanswered = 0
        if outcome == IDLE:
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
