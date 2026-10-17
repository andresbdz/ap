"""Rental units, decided by the client on 2026-10-17: Sale and Outright price NA.

A rental's page shows a rental fee where a sale price would be. Written into the
Sale price column it read as a price the part could be bought for.
"""

from __future__ import annotations

from txtav import db, exporter, extract, rules

RENTAL_PAGE = """<html><body>
<h1 class="part-detail-title">101-130003-183T</h1>
<div id="partDescription_0">PROPELLER ASSY</div>
<h2>Your Price: $4,725.00</h2>
<input id="YourPriceUS_0" value="4725.00"/>
<ul><li>Condition: <span>RENTAL</span></li></ul>
<p>1 IN STOCK</p>
</body></html>"""


class TestRecognisingARental:
    def test_the_page_marks_it_a_rental(self):
        record = extract.extract(RENTAL_PAGE, "101-130003-183T")
        assert record.availability == extract.RENTAL

    def test_it_is_no_longer_a_flag_for_review(self):
        # A known state with a decision behind it, not something to look at.
        record = extract.extract(RENTAL_PAGE, "101-130003-183T")
        assert record.conflicts == []

    def test_overhauled_counts_as_not_new(self):
        result = rules.condition_code("OVERHAULED", "123-4EX")
        assert result.code == "OH" and not result.conflict


class TestWhatTheSheetShows:
    def test_sale_and_outright_read_na(self, tmp_path):
        conn = db.connect(tmp_path / "t.db")
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO parts(pn, dedupe_key, wh, source_row, output_row)"
                " VALUES ('R-1T', 'R-1T', 'TXT', 2, 2)"
            )
            conn.execute("INSERT INTO cycles(id) VALUES (1)")
            conn.execute(
                "INSERT INTO results(cycle_id, pn, availability, sale_price, outright_price, core_value)"
                " VALUES (1, 'R-1T', 'rental', '4725.00', '4725.00', '0.00')"
            )
        row = dict(zip(exporter.COLUMNS, exporter.all_rows(conn)[0]))
        conn.close()

        assert row["Sale price"] == "NA"
        assert row["Outright price"] == "NA"
        assert row["Core value"] == 0, "everything else is written as usual"

    def test_the_fee_is_kept_in_the_store(self, tmp_path):
        # The sheet says NA, but the fee itself is not thrown away, in case
        # anyone later wants it.
        record = extract.extract(RENTAL_PAGE, "101-130003-183T")
        assert str(record.sale_price) == "4725.00"
