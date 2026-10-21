"""One part, one page load.

This is the only module that drives the browser. It owns the persistent
context, fetches a single part detail page, and hands the result to the guard
before anyone is allowed to look at the content. Nothing here decides what to
do about a halt; it reports and the runner acts.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from txtav import guard, redact_page, urls


@dataclass
class Fetch:
    pn: str
    url: str
    status: int | None
    final_url: str
    html: str
    text: str
    decision: guard.Decision
    # Whether the page reached a state worth reading: a part heading, the not
    # found notice, or the sign in link.
    settled: bool = True
    # Time to DOMContentLoaded, then time for the content to appear. Logged for
    # every part, because the pace is only defensible against measured numbers.
    nav_seconds: float = 0.0
    settle_seconds: float = 0.0
    # The raw Retry-After header, when the portal sent one.
    retry_after: str | None = None

    @property
    def seconds(self) -> float:
        return self.nav_seconds + self.settle_seconds


class Navigator:
    """Wraps a Playwright page. One instance per run, one tab, never parallel."""

    def __init__(self, page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url

    # The part number heading, the not found message, or the sign in link.
    # One of these is present once a part page has finished rendering.
    SETTLED = (
        "h1.part-detail-title, "
        ":text('Part Not Found'), "
        ":text('Hello, Log In')"
    )
    SETTLE_TIMEOUT_MS = 15_000

    def _settle(self, selector: str = SETTLED) -> bool:
        """Wait for the page to be readable. True if it got there in time.

        Not networkidle: Playwright marks it discouraged, and it never fires on
        this portal's dashboard, which keeps background connections open. A
        locator rather than wait_for_selector, which Playwright also marks
        discouraged in favour of Locator.wait_for. `.first` because a page can
        match more than one of the alternatives, and a locator is strict.

        The result is returned, not swallowed. Swallowing it meant a page still
        rendering when the wait ran out went on to the extractor, which found no
        part heading and raised a halt that only a person could clear: a slow
        page stopped the whole run as though the portal had changed.
        """
        try:
            self.page.locator(selector).first.wait_for(
                state="visible", timeout=self.SETTLE_TIMEOUT_MS
            )
            return True
        except Exception:  # noqa: BLE001 - absence is a verdict, not a crash
            return False

    def fetch(self, pn: str) -> Fetch:
        """Load one part detail page and judge it. Raises nothing on a bad page.

        Transport faults do propagate: the runner decides whether to retry them,
        because only it knows how many attempts this part has already had.
        """
        url = urls.detail_url(pn, self.base_url)
        started = time.monotonic()
        response = self.page.goto(url, wait_until="domcontentloaded")
        loaded = time.monotonic()
        settled = self._settle()
        nav_seconds, settle_seconds = loaded - started, time.monotonic() - loaded

        status = response.status if response is not None else None
        retry_after = response.header_value("retry-after") if response is not None else None
        html = self.page.content()
        text = self.page.inner_text("body")

        # The guard always judges first. A CAPTCHA or a 403 page carries none of
        # the settle markers, and must still halt rather than be retried.
        decision = guard.assess(status, self.page.url, text)
        if decision.kind == "rate_limited" and retry_after:
            decision = guard.Decision(
                decision.verdict,
                decision.kind,
                f"{decision.detail}; the portal asked us to wait (Retry-After: {retry_after})",
            )
        elif decision.verdict is guard.Verdict.OK and not settled:
            decision = guard.Decision(
                guard.Verdict.RETRY,
                "page_not_settled",
                f"no part heading, not found notice or sign in link within "
                f"{self.SETTLE_TIMEOUT_MS // 1000}s at {self.page.url}",
            )
        return Fetch(
            pn,
            url,
            status,
            self.page.url,
            html,
            text,
            decision,
            settled=settled,
            nav_seconds=nav_seconds,
            settle_seconds=settle_seconds,
            retry_after=retry_after,
        )

    def check_session(self) -> guard.Decision:
        """Judge the session from the dashboard, without looking up a part.

        Four answers, not two. OK means signed in. REAUTH means signed out. HALT
        means the dashboard itself carried a stop signal, a CAPTCHA or a 403,
        which must be treated as one. RETRY means the portal could not be
        reached at all: before this was told apart, a network drop of a minute
        at a pass boundary was reported as an expired session and sent someone
        to sign in again for nothing.
        """
        dashboard = f"{self.base_url.rstrip('/')}/Dashboard/"
        try:
            response = self.page.goto(dashboard, wait_until="domcontentloaded")
            self._settle("#main, header, body")
            text = self.page.inner_text("body")
        except Exception as exc:  # noqa: BLE001
            from txtav import logging_setup

            detail = f"{type(exc).__name__}: {exc}"
            logging_setup.get_logger("txtav.navigator").warning(
                "session_check_failed", error=detail
            )
            transport = guard.assess_transport(exc)
            if transport.kind in ("browser_gone", "offline"):
                return transport
            return guard.Decision(guard.Verdict.RETRY, "portal_unreachable", detail)
        status = response.status if response is not None else None
        return guard.assess(status, self.page.url, text)

    def is_signed_in(self) -> bool:
        """True only when the dashboard loaded and shows a signed in session."""
        return self.check_session().verdict is guard.Verdict.OK

    def screenshot(self, path: Path) -> dict[str, int]:
        """Capture the viewport for debugging a failure, with identities hidden.

        The account holder's name, the customer number and the support contact
        details are masked in the page before the shutter, so they never reach
        the image file. Returns a count per group, which the caller checks: a
        group matching nothing means the portal's markup changed and that value
        would otherwise be carried into a ticket.

        Viewport only, never full page, for the same reason.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        try:
            counts = redact_page.mask(self.page)
            self.page.screenshot(path=str(path), full_page=False)
        finally:
            try:
                redact_page.unmask(self.page)
            except Exception:  # noqa: BLE001 - the page may already be gone
                pass
        return counts


@contextmanager
def browser_session(
    profile_dir: Path | str,
    timeout_seconds: int,
    *,
    headless: bool = False,
    channel: str = "",
):
    """Open the persistent profile and yield a Navigator.

    Headed by default. A headless run presents a different browser build and a
    different user agent to the portal than the session was created with, and
    the whole approach here is that the session is an ordinary one.

    `channel` picks an installed browser instead of Playwright's own Chromium,
    e.g. "chrome". Needed on macOS 13, where Playwright no longer ships a
    Chromium build ("Playwright does not support chromium on mac13-arm64",
    seen on the run machine on 2026-10-21).
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir), headless=headless, viewport=None, channel=channel or None
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(timeout_seconds * 1000)
            yield page
        finally:
            context.close()
