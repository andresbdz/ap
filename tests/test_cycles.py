"""Cycle rollover and schedule adherence.

The scope asks for the same list to be searched again every 14 days, so a
cycle is a window rather than a queue. Before this was implemented nothing
ever marked a cycle finished: the first stayed open forever, a second never
started, and the system would have searched the list once and then sat idle.
"""

from __future__ import annotations

import sqlite3

import pytest

from txtav import db, runner

CYCLE_DAYS = 14


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    connection = db.connect(tmp_path / "t.db")
    with db.transaction(connection):
        connection.executemany(
            "INSERT INTO parts(pn, dedupe_key, wh, source_row, output_row)"
            " VALUES (?,?,'TXT',?,?)",
            [(f"P{i}", f"P{i}", i + 200, i + 1) for i in range(1, 6)],
        )
    yield connection
    connection.close()


def finish_all(conn: sqlite3.Connection, cycle_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'success', finished_at = datetime('now')"
        " WHERE cycle_id = ?",
        (cycle_id,),
    )


class TestStartingACycle:
    def test_first_call_opens_a_cycle_and_queues_every_part(self, conn):
        cycle_id = runner.current_cycle(conn, CYCLE_DAYS)
        queued = conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE cycle_id = ?", (cycle_id,)
        ).fetchone()["n"]
        assert queued == 5

    def test_an_unfinished_cycle_is_reused(self, conn):
        first = runner.current_cycle(conn, CYCLE_DAYS)
        assert runner.current_cycle(conn, CYCLE_DAYS) == first
        assert conn.execute("SELECT COUNT(*) n FROM cycles").fetchone()["n"] == 1


class TestFinishingACycle:
    def test_a_cycle_with_work_left_stays_open(self, conn):
        cycle_id = runner.current_cycle(conn, CYCLE_DAYS)
        assert runner.finish_cycle_if_complete(conn, cycle_id) is False
        assert conn.execute(
            "SELECT status FROM cycles WHERE id = ?", (cycle_id,)
        ).fetchone()["status"] == "running"

    def test_a_cycle_closes_once_nothing_is_pending(self, conn):
        cycle_id = runner.current_cycle(conn, CYCLE_DAYS)
        finish_all(conn, cycle_id)

        assert runner.finish_cycle_if_complete(conn, cycle_id) is True
        row = conn.execute(
            "SELECT status, finished_at FROM cycles WHERE id = ?", (cycle_id,)
        ).fetchone()
        assert row["status"] == "finished"
        assert row["finished_at"] is not None

    def test_closing_is_recorded_as_an_event(self, conn):
        cycle_id = runner.current_cycle(conn, CYCLE_DAYS)
        finish_all(conn, cycle_id)
        runner.finish_cycle_if_complete(conn, cycle_id)
        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM events")}
        assert "cycle_finished" in kinds


class TestRollover:
    def test_finishing_early_waits_rather_than_starting_again(self, conn):
        # Searching the list twice inside one window would double the load on
        # the portal, which is the one thing this project cannot do.
        cycle_id = runner.current_cycle(conn, CYCLE_DAYS)
        finish_all(conn, cycle_id)

        assert runner.current_cycle(conn, CYCLE_DAYS) is None
        assert conn.execute("SELECT COUNT(*) n FROM cycles").fetchone()["n"] == 1

    def test_a_new_cycle_starts_once_the_window_has_passed(self, conn):
        first = runner.current_cycle(conn, CYCLE_DAYS)
        finish_all(conn, first)
        runner.finish_cycle_if_complete(conn, first)
        conn.execute(
            "UPDATE cycles SET ends_at = datetime('now', '-1 day') WHERE id = ?",
            (first,),
        )

        second = runner.current_cycle(conn, CYCLE_DAYS)
        assert second is not None and second != first
        assert conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE cycle_id = ?", (second,)
        ).fetchone()["n"] == 5

    def test_the_same_parts_are_queued_again(self, conn):
        first = runner.current_cycle(conn, CYCLE_DAYS)
        finish_all(conn, first)
        runner.finish_cycle_if_complete(conn, first)
        conn.execute("UPDATE cycles SET ends_at = datetime('now', '-1 day')")

        second = runner.current_cycle(conn, CYCLE_DAYS)
        pns = {r["pn"] for r in conn.execute(
            "SELECT pn FROM jobs WHERE cycle_id = ?", (second,)
        )}
        assert pns == {f"P{i}" for i in range(1, 6)}


class TestScheduleAdherence:
    def test_a_fresh_cycle_is_on_schedule(self, conn):
        cycle_id = runner.current_cycle(conn, CYCLE_DAYS)
        progress = runner.cycle_progress(conn, cycle_id)
        assert progress.total == 5
        assert progress.done == 0
        assert progress.on_schedule

    def test_half_elapsed_with_nothing_done_is_behind(self, conn):
        cycle_id = runner.current_cycle(conn, CYCLE_DAYS)
        # Both ends move, so the window stays 14 days long and exactly half
        # of it has elapsed.
        conn.execute(
            "UPDATE cycles SET started_at = datetime('now', '-7 days'),"
            " ends_at = datetime('now', '+7 days') WHERE id = ?",
            (cycle_id,),
        )
        progress = runner.cycle_progress(conn, cycle_id)
        assert progress.expected_done == 2
        assert progress.behind_by == 2
        assert not progress.on_schedule
        assert "behind by" in progress.describe()

    def test_half_elapsed_and_half_done_is_on_schedule(self, conn):
        cycle_id = runner.current_cycle(conn, CYCLE_DAYS)
        # Both ends move, so the window stays 14 days long and exactly half
        # of it has elapsed.
        conn.execute(
            "UPDATE cycles SET started_at = datetime('now', '-7 days'),"
            " ends_at = datetime('now', '+7 days') WHERE id = ?",
            (cycle_id,),
        )
        conn.execute(
            "UPDATE jobs SET status = 'success' WHERE cycle_id = ? AND pn IN ('P1','P2','P3')",
            (cycle_id,),
        )
        assert runner.cycle_progress(conn, cycle_id).on_schedule

    def test_an_unknown_cycle_has_no_progress(self, conn):
        assert runner.cycle_progress(conn, 999) is None
