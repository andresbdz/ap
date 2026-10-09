"""Last Updated, Last Checked and Last Scraped.

The portal shows no date of its own on a part page, so all three are this
system's own record. What matters is that each answers a different question:
how long a value has stood, when we last looked, and when we last read data.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from txtav import db, exporter, extract, runner


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    with db.transaction(connection):
        connection.execute(
            "INSERT INTO parts(pn, dedupe_key, wh, source_row, output_row)"
            " VALUES ('A-1', 'A-1', 'TXT', 2, 2)"
        )
        connection.execute("INSERT INTO cycles(id) VALUES (1)")
        connection.execute("INSERT INTO cycles(id) VALUES (2)")
    yield connection
    connection.close()


def record(price: str, qty: int = 2) -> extract.PartRecord:
    return extract.PartRecord(
        pn_requested="A-1",
        pn_resolved="A-1",
        found=True,
        description="BRACKET",
        quantity=qty,
        sale_price=Decimal(price),
    )


def store_at(conn, cycle_id: int, rec, when: str) -> None:
    runner._store(conn, cycle_id, rec)
    conn.execute(
        "UPDATE results SET scraped_at = ?,"
        " updated_at = CASE WHEN updated_at >= datetime('now', '-1 minute') THEN ? ELSE updated_at END"
        " WHERE cycle_id = ? AND pn = 'A-1'",
        (when, when, cycle_id),
    )


def row(conn) -> dict:
    values = exporter.all_rows(conn)[0]
    return dict(zip(exporter.COLUMNS, values))


class TestLastUpdated:
    def test_unchanged_values_keep_the_original_date(self, conn):
        store_at(conn, 1, record("10.00"), "2026-09-01 08:00:00")
        store_at(conn, 2, record("10.00"), "2026-09-15 08:00:00")

        r = row(conn)
        assert r["Last Updated"] == "2026-09-01 08:00", "nothing changed, so it still stands from the 1st"
        assert r["Last Scraped"] == "2026-09-15 08:00"

    def test_a_price_change_moves_it(self, conn):
        store_at(conn, 1, record("10.00"), "2026-09-01 08:00:00")
        store_at(conn, 2, record("12.50"), "2026-09-15 08:00:00")

        assert row(conn)["Last Updated"] == "2026-09-15 08:00"

    def test_a_stock_change_moves_it_too(self, conn):
        store_at(conn, 1, record("10.00", qty=2), "2026-09-01 08:00:00")
        store_at(conn, 2, record("10.00", qty=0), "2026-09-15 08:00:00")

        assert row(conn)["Last Updated"] == "2026-09-15 08:00"

    def test_rows_from_before_the_column_existed_fall_back_to_scraped(self, conn):
        store_at(conn, 1, record("10.00"), "2026-08-20 08:00:00")
        conn.execute("UPDATE results SET updated_at = NULL")

        assert row(conn)["Last Updated"] == "2026-08-20 08:00"


class TestLastChecked:
    def test_a_failed_lookup_counts_as_checked_but_not_scraped(self, conn):
        store_at(conn, 1, record("10.00"), "2026-09-01 08:00:00")
        conn.execute(
            "INSERT INTO jobs(cycle_id, pn, status, finished_at)"
            " VALUES (1, 'A-1', 'success', '2026-09-01 08:00:00'),"
            "        (2, 'A-1', 'failed',  '2026-09-15 09:30:00')"
        )

        r = row(conn)
        assert r["Last Checked"] == "2026-09-15 09:30", "we did look"
        assert r["Last Scraped"] == "2026-09-01 08:00", "but read nothing new"

    def test_a_part_not_yet_reached_has_no_dates(self, conn):
        r = row(conn)
        assert r["Last Updated"] == r["Last Checked"] == r["Last Scraped"] == ""

    def test_a_part_still_queued_is_not_checked(self, conn):
        conn.execute(
            "INSERT INTO jobs(cycle_id, pn, status, finished_at)"
            " VALUES (1, 'A-1', 'pending', '2026-09-01 08:00:00')"
        )
        assert row(conn)["Last Checked"] == ""
