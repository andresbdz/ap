"""Tests for failure modes found by reading the code rather than by running it.

Each one pins a behaviour that was wrong in a way no passing test would have
shown: a promise the pacer did not keep, a halt filed under a misleading cause,
a command that could undo a day's repair, and a schedule check that only worked
for a fourteen day cycle.
"""

from __future__ import annotations

import argparse
import sqlite3

import pytest

from txtav import db, guard, runner, sheet_commands
from txtav.config import Settings


class TestADeadBrowserIsNotARefusal:
    """Our own process failing must not be filed next to a 403.

    The halt log is what a post mortem is built from, and an operator reading
    "unexpected_error" goes looking for a block. There was none: Chromium died.
    """

    @pytest.mark.parametrize(
        "message",
        [
            "Target page, context or browser has been closed",
            "Browser has been closed",
            "Target closed",
        ],
    )
    def test_a_closed_browser_gets_its_own_kind(self, message):
        decision = guard.assess_transport(RuntimeError(message))

        assert decision.kind == "browser_gone"
        assert decision.should_stop, "nothing may run against a browser that is gone"
        assert "not the portal" in decision.detail

    def test_a_genuinely_unknown_fault_still_halts_as_unknown(self):
        decision = guard.assess_transport(RuntimeError("something nobody has seen"))
        assert decision.kind == "unexpected_error"


class TestScheduleSlackFollowsTheCycle:
    """The slack was `total / (14 * 24)` whatever the cycle length was.

    A seven day cycle has to run twice as fast, so an hour of it is half as
    many parts, and the fixed divisor called a late run healthy.
    """

    def _progress(self, total_days: float, behind: int) -> runner.CycleProgress:
        total = 109_589
        done = total - behind
        return runner.CycleProgress(
            cycle_id=1,
            total=total,
            done=done,
            elapsed_fraction=1.0,
            ends_at="2026-10-20 00:00:00",
            total_days=total_days,
        )

    def test_an_hour_behind_is_tolerated_on_a_fourteen_day_cycle(self):
        assert self._progress(14.0, behind=300).on_schedule

    def test_the_same_gap_is_not_tolerated_on_a_longer_cycle(self):
        # Same parts over 28 days is half the hourly rate, so an hour of slack
        # is half as many parts and 300 behind is now most of two hours.
        assert not self._progress(28.0, behind=300).on_schedule

    def test_a_cycle_with_no_length_is_not_reported_as_late(self):
        assert self._progress(0.0, behind=50_000).on_schedule


class TestSheetPushRefusesAPartialTable:
    """`--processed-only` would shift every row and shrink the tab.

    Writing only the processed parts contiguously from row 2 breaks the one
    invariant the Output tab has, that row N means part N. It is how 1,148 rows
    came to hold another part's prices on 2026-10-06, and it was reachable from
    a documented flag.
    """

    def test_it_refuses_and_touches_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("TXTAV_SHEET_ID", "sid")
        monkeypatch.setenv("TXTAV_DB_PATH", str(tmp_path / "t.db"))

        def must_not_be_called(*args, **kwargs):
            raise AssertionError("it authorised against the live sheet")

        monkeypatch.setattr(sheet_commands.sheets, "authorise", must_not_be_called)

        args = argparse.Namespace(
            dry_run=False, repair_header=False, processed_only=True
        )
        assert sheet_commands.cmd_sheet_push(args) == 1
        assert "txtav export --processed-only" in capsys.readouterr().out


class TestTheStoreSurvivesAReimportMidCycle:
    """A re-import used to wipe the parts table, which a live cycle forbids.

    jobs and results both reference parts(pn), so the delete failed the foreign
    key check; and before the pn indexes existed it did not fail, it hung,
    because pn is not the leading column of either primary key.
    """

    def test_the_foreign_key_check_has_an_index_to_use(self, tmp_path):
        conn = db.connect(tmp_path / "t.db")
        try:
            indexes = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            assert "jobs_pn" in indexes
            assert "results_pn" in indexes
        finally:
            conn.close()

    def test_foreign_keys_are_actually_enforced(self, tmp_path):
        # Off by default in SQLite, and silently so.
        conn = db.connect(tmp_path / "t.db")
        try:
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            conn.execute("INSERT INTO cycles(id) VALUES (1)")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO jobs(cycle_id, pn) VALUES (1, 'NOT-A-PART')")
        finally:
            conn.close()


class TestConfiguredDefaults:
    def test_the_failure_tripwire_leaves_room_for_a_network_wobble(self):
        # Every counted failure is already three exhausted attempts of its own,
        # and a halt costs a person. At 3 a brief drop overnight stopped the run
        # until morning. Read off the field rather than an instance, because an
        # instance would be answering for whatever the untracked .env says.
        default = Settings.model_fields["consecutive_failure_halt"].default
        assert 5 <= default <= 12

    def test_the_interval_cannot_be_turned_into_a_hammer(self):
        with pytest.raises(ValueError):
            Settings(request_interval_seconds=0.5)
