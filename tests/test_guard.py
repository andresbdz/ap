"""Stop condition tests.

The bias under test is deliberate: when in doubt, halt. A false halt costs one
message to an operator; a missed one costs the client's account.
"""

from __future__ import annotations

import pytest

from pathlib import Path

from txtav import extract, guard

URL = "https://ww2.txtav.com/Parts/PartSearch/PartsDetail/6622168-22"
GOOD_PAGE = "6622168-22 BLEED AIR STOP-RH Your Price: $357.08 2 IN STOCK"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class TestRealPagesAreNotMistakenForRefusals:
    """The regression that matters most.

    The portal loads reCAPTCHA on every page, so "captcha" appears 69 times in
    the markup of an ordinary part page. Judging a page by its raw HTML halts
    the run on the first part, every time. These assert on the visible text,
    which is what the Navigator actually passes in.
    """

    @pytest.mark.skipif(
        not (FIXTURES / "6622168-22.html").exists(), reason="fixtures not captured"
    )
    @pytest.mark.parametrize(
        "name", ["6622168-22", "260-1014-33", "9912125-2EX", "ZZZZ-9999"]
    )
    def test_a_real_page_passes(self, name):
        html = (FIXTURES / f"{name}.html").read_text(encoding="utf-8")
        assert guard.assess(200, URL, extract.visible_text(html)).verdict is guard.Verdict.OK

    @pytest.mark.skipif(
        not (FIXTURES / "6622168-22.html").exists(), reason="fixtures not captured"
    )
    def test_raw_markup_would_have_tripped_the_captcha_rule(self, name="6622168-22"):
        # Documents why visible_text exists at all.
        html = (FIXTURES / f"{name}.html").read_text(encoding="utf-8")
        assert "captcha" in html.lower()
        assert "captcha" not in extract.visible_text(html).lower()


class TestNormalPage:
    def test_a_good_page_passes(self):
        assert guard.assess(200, URL, GOOD_PAGE).verdict is guard.Verdict.OK

    def test_a_not_found_page_is_not_a_halt(self):
        # A part that does not exist is a normal outcome, not a refusal.
        decision = guard.assess(200, URL, "ZZZZ-9999 Part Not Found.")
        assert decision.verdict is guard.Verdict.OK


class TestRefusals:
    @pytest.mark.parametrize("status,kind", [(401, "unauthorized"), (403, "forbidden"), (429, "rate_limited")])
    def test_refusal_statuses_halt(self, status, kind):
        decision = guard.assess(status, URL, GOOD_PAGE)
        assert decision.verdict is guard.Verdict.HALT
        assert decision.kind == kind

    @pytest.mark.parametrize(
        "phrase",
        [
            "Please complete the CAPTCHA to continue",
            "We have detected unusual activity from your account",
            "Access Denied",
            "Your account has been locked",
            "Too Many Requests",
            "Verify you are human",
        ],
    )
    def test_warning_phrases_halt(self, phrase):
        assert guard.assess(200, URL, phrase).verdict is guard.Verdict.HALT

    def test_losing_pricing_access_halts(self):
        # Silently recording blanks for 109,589 parts because pricing was
        # revoked would destroy the sheet. Stop instead.
        decision = guard.assess(200, URL, "Request Access to View Pricing and Availability")
        assert decision.verdict is guard.Verdict.HALT
        assert decision.kind == "pricing_revoked"

    def test_a_refusal_outranks_a_login_redirect(self):
        decision = guard.assess(403, "https://ww2.txtav.com/Account/Login", "Hello, Log In!")
        assert decision.verdict is guard.Verdict.HALT


class TestSessionExpiry:
    def test_login_redirect_asks_for_reauth(self):
        decision = guard.assess(200, "https://ww2.txtav.com/Account/Login", "Sign in")
        assert decision.verdict is guard.Verdict.REAUTH

    def test_logged_out_header_asks_for_reauth(self):
        decision = guard.assess(200, URL, "Hello, Log In! My Account Log In Register")
        assert decision.verdict is guard.Verdict.REAUTH

    def test_reauth_counts_as_stopping(self):
        assert guard.assess(200, URL, "Hello, Log In!").should_stop


class TestTransientFaults:
    def test_server_errors_are_retried_not_halted(self):
        assert guard.assess(503, URL, "").verdict is guard.Verdict.RETRY

    def test_timeouts_are_retried(self):
        decision = guard.assess_transport(TimeoutError("Timeout 45000ms exceeded"))
        assert decision.verdict is guard.Verdict.RETRY

    def test_connection_resets_are_retried(self):
        decision = guard.assess_transport(OSError("net::ERR_CONNECTION_RESET"))
        assert decision.verdict is guard.Verdict.RETRY

    def test_an_unrecognised_exception_halts_rather_than_guessing(self):
        decision = guard.assess_transport(ValueError("something nobody anticipated"))
        assert decision.verdict is guard.Verdict.HALT


class TestConsecutiveFailureTripwire:
    def test_stays_quiet_below_the_limit(self):
        tripwire = guard.ConsecutiveFailureTripwire(limit=3)
        assert tripwire.record_failure().verdict is guard.Verdict.OK
        assert tripwire.record_failure().verdict is guard.Verdict.OK

    def test_trips_at_the_limit(self):
        tripwire = guard.ConsecutiveFailureTripwire(limit=3)
        tripwire.record_failure()
        tripwire.record_failure()
        decision = tripwire.record_failure()
        assert decision.verdict is guard.Verdict.HALT
        assert decision.kind == "consecutive_failures"

    def test_a_success_resets_the_run(self):
        tripwire = guard.ConsecutiveFailureTripwire(limit=3)
        tripwire.record_failure()
        tripwire.record_failure()
        tripwire.record_success()
        assert tripwire.record_failure().verdict is guard.Verdict.OK
