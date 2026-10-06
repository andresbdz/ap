"""The run loop must never leave without recording why.

On its way out, `run` writes a halt, flushes the sheet and sends an alert. An
exception escaping it does none of those. On 2026-10-06 a page whose price
heading read "Your Price: Call for pricing" killed the process with a traceback:
the part was left in_flight, no halt was recorded and nobody was told. These
tests pin the shape of that exit, and the carrying of the pacer and the tripwire
across pass boundaries, which the supervisor depends on.
"""

from __future__ import annotations

import test_runner
from test_runner import FakeNavigator, fixture_html, seed  # noqa: F401
from test_runner import conn, settings  # noqa: F401 - fixtures reused as is

from txtav import db, guard, runner

pytestmark = test_runner.pytestmark  # skipped alike when the fixtures are absent

UNREADABLE_PRICE = """<html><body>
<h1 class="part-detail-title">6622168-22</h1>
<div id="partDescription_0">BRACKET ASSY</div>
<h2>Your Price: Call for pricing</h2>
<p>2 IN STOCK</p>
</body></html>"""


class TestNothingLeavesTheLoopUnrecorded:
    def test_an_unreadable_field_halts_instead_of_crashing(self, conn, settings):
        # rules.FieldError and extract.ExtractionError are both ValueError and
        # neither derives from the other, so catching one alone let the other out.
        seed(conn, ["6622168-22"])
        nav = FakeNavigator({"6622168-22": UNREADABLE_PRICE})

        report = runner.run(conn, nav, settings)

        assert report.halted is not None, "the run must stop"
        assert report.halted.kind == "unrecognised_page"
        assert db.open_halt(conn) is not None, "and write down why"

    def test_an_extractor_bug_also_halts(self, conn, settings, monkeypatch):
        seed(conn, ["6622168-22"])
        nav = FakeNavigator({"6622168-22": fixture_html("6622168-22")})

        def boom(html, pn):
            raise KeyError("a selector the extractor assumed was there")

        monkeypatch.setattr(runner.extract, "extract", boom)
        report = runner.run(conn, nav, settings)

        assert report.halted is not None
        assert report.halted.kind == "extractor_failed"
        assert db.open_halt(conn) is not None


class TestPacingAndTripwireSurvivePassBoundaries:
    def test_a_supplied_tripwire_counts_across_passes(self, conn, settings):
        # Rebuilt per pass, a drip of failures either side of a boundary never
        # tripped anything: the count reset every 200 parts.
        settings.consecutive_failure_halt = 2
        tripwire = guard.ConsecutiveFailureTripwire(settings.consecutive_failure_halt)
        pns = ["6622168-22", "260-1014-33"]
        seed(conn, pns)
        timeout = TimeoutError("Timeout 45000ms exceeded")
        nav = FakeNavigator(
            {pn: fixture_html(pn) for pn in pns}, raises={pn: timeout for pn in pns}
        )

        first = runner.run(conn, nav, settings, limit=1, tripwire=tripwire)
        assert first.halted is None, "one failed part is not a pattern"

        second = runner.run(conn, nav, settings, limit=1, tripwire=tripwire)
        assert second.halted is not None
        assert second.halted.kind == "consecutive_failures"

    def test_a_supplied_pacer_paces_every_pass(self, conn, settings):
        # The first wait of a fresh pacer is free, so one request per pass went
        # out without honouring the interval.
        waits: list[int] = []

        class CountingPacer(runner.Pacer):
            def wait(self) -> float:
                waits.append(1)
                return 0.0

        pns = ["6622168-22", "260-1014-33"]
        seed(conn, pns)
        nav = FakeNavigator({pn: fixture_html(pn) for pn in pns})
        pace = CountingPacer(0.0, 0.0)

        runner.run(conn, nav, settings, limit=1, pace=pace)
        runner.run(conn, nav, settings, limit=1, pace=pace)

        assert len(waits) == 2, "the same pacer was asked both times"
