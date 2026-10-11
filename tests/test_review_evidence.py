"""Findings from the first 19,404 parts of the live run, 2026-10-11.

- "1001  -  HINGE" was flagged as a different part from its own page, which
  reads "1001 - HINGE": only the spacing differed.
- Flagged pages were not kept, so three parts whose displayed price sat 13.32 %
  above the hidden one could be described but not settled.
- A sheet write that timed out and was then written by the next flush still
  raised an alert telling people to run a push nobody needed.
"""

from __future__ import annotations

from pathlib import Path

from txtav import db, extract, runner

from test_runner import FakeNavigator, fixture_html, seed  # noqa: F401
from test_runner import conn, settings  # noqa: F401 - fixtures


class TestSpacingIsNotADifferentPart:
    def test_repeated_spaces_match(self):
        assert extract._same_pn("1001  -  HINGE", "1001 - HINGE")

    def test_a_different_part_still_does_not(self):
        assert not extract._same_pn("1001-HINGE", "1002-HINGE")


class TestFlaggedPagesAreKept:
    def test_a_rare_flag_keeps_its_page(self, conn, settings, tmp_path, monkeypatch):
        settings.review_pages_dir = tmp_path / "review"
        seed(conn, ["6622168-22"])
        nav = FakeNavigator({"6622168-22": fixture_html("6622168-22")})

        real = extract.extract

        def flagged(html, pn):
            record = real(html, pn)
            record.conflicts.append("hidden price 1157.9 does not match the displayed Your Price 1312.12")
            return record

        monkeypatch.setattr(runner.extract, "extract", flagged)
        runner.run(conn, nav, settings)

        assert (settings.review_pages_dir / "6622168-22.html").exists()

    def test_multiple_alternates_alone_are_not_kept(self):
        # Common, and fully described by the flag itself.
        assert not runner._worth_keeping(["more than one alternate part number is visible: A=1, B=2"])
        assert runner._worth_keeping(["unrecognised condition 'RENTAL' on page"])


class TestASelfHealingSheetWriteIsNotAlerted:
    def test_a_pending_retry_is_recorded_not_sent(self, conn, settings, monkeypatch):
        from txtav import live_sheet

        sent = []
        monkeypatch.setattr(runner.alerts, "send", lambda hook, title, body: sent.append(title))

        class OneFailure(live_sheet.LiveSheet):
            def flush(self, conn):
                self.consecutive_failures = 1
                return 0

        monkeypatch.setattr(runner.live_sheet, "build", lambda s: OneFailure(None, "sid", "Output"))
        seed(conn, ["6622168-22"])
        runner.run(conn, FakeNavigator({"6622168-22": fixture_html("6622168-22")}), settings)

        assert "TXT sheet is behind the database" not in sent
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events")]
        assert "sheet_retry_pending" in kinds
