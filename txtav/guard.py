"""Stop conditions.

Losing access to this account would cost the business its ability to buy
parts from this supplier, so the expensive failure is not a crash, it is
carrying on after the portal has asked us to stop. Everything in here
therefore errs towards halting.

A halt is global and sticky. It is written to the database, it blocks every
subsequent run, and only an operator can clear it. Nothing in this module or
anywhere else retries past one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Verdict(str, Enum):
    OK = "ok"
    HALT = "halt"            # the portal signalled a problem: stop entirely
    REAUTH = "reauth"        # the session died: pause and ask for a human
    RETRY = "retry"          # ordinary transport fault: back off and try again


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    kind: str
    detail: str

    @property
    def should_stop(self) -> bool:
        return self.verdict in (Verdict.HALT, Verdict.REAUTH)


OK = Decision(Verdict.OK, "ok", "")

# HTTP statuses that mean the portal is refusing us rather than failing.
HALT_STATUSES = {401: "unauthorized", 403: "forbidden", 429: "rate_limited"}

# Phrases that indicate automation is unwelcome or access has been restricted.
# Matched against the page's visible text, lower cased.
HALT_PHRASES = (
    ("captcha", "captcha"),
    ("i'm not a robot", "captcha"),
    ("are you a robot", "captcha"),
    ("verify you are human", "captcha"),
    ("unusual activity", "access_warning"),
    ("suspicious activity", "access_warning"),
    ("automated access", "access_warning"),
    ("access denied", "access_denied"),
    ("account has been locked", "account_locked"),
    ("account is locked", "account_locked"),
    ("temporarily blocked", "access_denied"),
    ("too many requests", "rate_limited"),
    ("request access to view pricing", "pricing_revoked"),
)

# Signs the session has lapsed. Recoverable, but only by a human signing in.
REAUTH_PHRASES = ("hello, log in", "my account log in register", "session has expired")
REAUTH_URL_MARKERS = ("/account/login", "/account/signin", "/login?", "idp.", "okta")

# Playwright's wording when the browser, context or page has died under us.
# This is our own process failing, not the portal, and it should not be filed
# alongside a 403 in the halt log.
BROWSER_GONE = (
    "target page, context or browser has been closed",
    "target closed",
    "browser has been closed",
    "browser has disconnected",
    "browser closed",
)

_WS = re.compile(r"\s+")


def _flatten(text: str) -> str:
    return _WS.sub(" ", text or "").strip().lower()


def assess(status: int | None, url: str, page_text: str) -> Decision:
    """Judge one page load. Called for every single request, before extraction.

    Order matters: an outright refusal outranks a login redirect, because a
    403 served on the login page is still a 403.
    """
    if status is not None and status in HALT_STATUSES:
        return Decision(
            Verdict.HALT,
            HALT_STATUSES[status],
            f"portal returned HTTP {status} for {url}",
        )

    body = _flatten(page_text)
    for phrase, kind in HALT_PHRASES:
        if phrase in body:
            return Decision(Verdict.HALT, kind, f"page contains {phrase!r}")

    lowered_url = (url or "").lower()
    if any(marker in lowered_url for marker in REAUTH_URL_MARKERS):
        return Decision(Verdict.REAUTH, "session_expired", f"redirected to {url}")
    for phrase in REAUTH_PHRASES:
        if phrase in body:
            return Decision(Verdict.REAUTH, "session_expired", f"page contains {phrase!r}")

    if status is not None and status >= 500:
        return Decision(Verdict.RETRY, "server_error", f"HTTP {status} for {url}")

    return OK


def assess_transport(exc: BaseException) -> Decision:
    """Judge an exception raised while loading a page.

    Timeouts and connection faults are ordinary and get a bounded retry.
    Everything else is unknown, and unknown means stop rather than guess.
    """
    name = type(exc).__name__
    message = str(exc).lower()

    # A dead browser, told apart from a refusal by the portal. Still a halt,
    # because nothing should carry on against a browser that is not there, but
    # under its own kind: the detail an operator reads should say "restart it"
    # rather than send them looking for a block that never happened.
    if any(marker in message for marker in BROWSER_GONE):
        return Decision(
            Verdict.HALT,
            "browser_gone",
            f"the browser went away, not the portal ({name}: {exc}). Nothing "
            f"suggests we were blocked; check the machine stayed up, then clear "
            f"this halt and start again. The run resumes where it stopped.",
        )

    transport = (
        "timeout",
        "net::err",
        "connection",
        "econnreset",
        "socket",
        "dns",
        "temporarily unavailable",
    )
    if any(marker in message for marker in transport) or "Timeout" in name:
        return Decision(Verdict.RETRY, "transport", f"{name}: {exc}")
    return Decision(Verdict.HALT, "unexpected_error", f"{name}: {exc}")


class ConsecutiveFailureTripwire:
    """Trips when too many parts fail in a row.

    A string of failures with no single obvious cause is the shape of being
    quietly throttled or served junk, which is exactly the case where carrying
    on does the most damage.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.count = 0

    def record_failure(self) -> Decision:
        self.count += 1
        if self.count >= self.limit:
            return Decision(
                Verdict.HALT,
                "consecutive_failures",
                f"{self.count} parts failed in a row, which usually means "
                f"silent blocking rather than {self.count} unrelated faults",
            )
        return OK

    def record_success(self) -> None:
        self.count = 0
