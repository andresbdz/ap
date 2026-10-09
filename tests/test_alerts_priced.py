"""'priced' in the daily summary means a part with an actual price.

A quote only or not offered part is read correctly and its job succeeds, but it
has no price. Counting job successes as "priced" overstated coverage by 353
parts on 2026-10-09, in a summary that goes to Slack and to the client.
"""

from __future__ import annotations

from txtav import alerts, db


def test_quote_only_and_not_offered_are_not_priced(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    with db.transaction(conn):
        conn.executemany(
            "INSERT INTO parts(pn, dedupe_key, wh, source_row, output_row)"
            " VALUES (?, ?, 'TXT', ?, ?)",
            [(pn, pn, i, i) for i, pn in enumerate(("P", "Q", "N"), start=2)],
        )
        conn.execute("INSERT INTO cycles(id) VALUES (1)")
        conn.executemany(
            "INSERT INTO jobs(cycle_id, pn, status) VALUES (1, ?, 'success')",
            [("P",), ("Q",), ("N",)],
        )
        conn.executemany(
            "INSERT INTO results(cycle_id, pn, availability, sale_price) VALUES (1, ?, ?, ?)",
            [
                ("P", "available", "10.00"),
                ("Q", "price_on_request", None),
                ("N", "no_price_listed", None),
            ],
        )

    report = alerts.build_daily_report(conn)
    conn.close()

    assert report.success == 3, "all three were read correctly"
    assert report.priced == 1, "but only one has a price"
    assert "priced        1" in report.text()
