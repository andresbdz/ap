"""One part, one page load.

This is the only module that drives the browser. It owns the persistent
context, fetches a single part detail page, and hands the result to the guard
before anyone is allowed to look at the content. Nothing here decides what to
do about a halt; it reports and the runner acts.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from txtav import guard, urls


@dataclass
class Fetch:
    pn: str
    url: str
    status: int | None
    final_url: str
    html: str
    text: str
    decision: guard.Decision


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

    def _settle(self, selector: str = SETTLED) -> None:
        """Wait for the page to be readable, without using networkidle.

        networkidle never fires on this portal's dashboard, which keeps
        background connections open, so a 45 second timeout was reached and
        thrown as a bare traceback. Waiting for the content we actually need is
        both faster and survivable: if the marker never appears we carry on and
        let the guard and the extractor judge whatever did load.
        """
        try:
            self.page.wait_for_selector(selector, timeout=self.SETTLE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - absence is a verdict, not a crash
            pass

    def fetch(self, pn: str) -> Fetch:
        """Load one part detail page and judge it. Raises nothing on a bad page.

        Transport faults do propagate: the runner decides whether to retry them,
        because only it knows how many attempts this part has already had.
        """
        url = urls.detail_url(pn, self.base_url)
        response = self.page.goto(url, wait_until="domcontentloaded")
        status = response.status if response is not None else None
        self._settle()

        html = self.page.content()
        text = self.page.inner_text("body")
        decision = guard.assess(status, self.page.url, text)
        return Fetch(pn, url, status, self.page.url, html, text, decision)

    def is_signed_in(self) -> bool:
        """Check the session without looking up a part.

        Returns False rather than raising on any fault. A session check that
        throws would take down a run that a plain "sign in again" would fix.
        """
        dashboard = f"{self.base_url.rstrip('/')}/Dashboard/"
        try:
            self.page.goto(dashboard, wait_until="domcontentloaded")
            self._settle("#main, header, body")
            text = self.page.inner_text("body")
        except Exception as exc:  # noqa: BLE001
            from txtav import logging_setup

            logging_setup.get_logger("txtav.navigator").warning(
                "session_check_failed", error=f"{type(exc).__name__}: {exc}"
            )
            return False
        decision = guard.assess(None, self.page.url, text)
        return decision.verdict is not guard.Verdict.REAUTH

    def screenshot(self, path: Path) -> None:
        """Capture the viewport for debugging a failure.

        Viewport only, never full page: the account name and the regional
        support contact block sit outside it, and a screenshot is the easiest
        way to leak personal data into a ticket.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=False)


@contextmanager
def browser_session(profile_dir: Path | str, timeout_seconds: int, *, headless: bool = False):
    """Open the persistent profile and yield a Navigator.

    Headed by default. A headless run presents a different browser build and a
    different user agent to the portal than the session was created with, and
    the whole approach here is that the session is an ordinary one.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir), headless=headless, viewport=None
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(timeout_seconds * 1000)
            yield page
        finally:
            context.close()
