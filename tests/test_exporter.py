"""Export tests.

The column order is the client's and is load bearing: they paste this into
their own sheet. The other thing under test is that a part with no price
produces an empty cell, never a zero.
"""

from __future__ import annotations

import csv
import sqlite3
from decimal import Decimal

import pytest

from txtav import db, exporter


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    connection = db.connect(tmp_path / "t.db")
    with db.transaction(connection):
        connection.executemany(
            "INSERT INTO parts(pn, dedupe_key, wh, source_row) VALUES (?,?,'TXT',?)",
            [
                ("9912125-2EX", "9912125-2EX", 1),
                ("6622168-22", "6622168-22", 2),
                ("000-110105-52U/P", "000-110105-52U/P", 3),
            ],
        )
        connection.execute("INSERT INTO cycles(id) VALUES (1)")
        connection.executemany(
            "INSERT INTO results(cycle_id, pn, description, availability, quantity,"
            " sale_price, condition_code, core_value, outright_price, part_type, alt_pn)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (1, "9912125-2EX", "STARTER GENERATOR", "available", 46,
                 "7724.69", "OH", "16500.00", "24224.69", "OEM Exchange", "9912125-2"),
                (1, "6622168-22", "BLEED AIR STOP-RH", "available", 2,
                 "357.08", "NE", "0.00", "357.08", "OEM Outright", None),
                (1, "000-110105-52U/P", "SPLICE- NOSE-W.S. 47.00", "no_longer_available",
                 None, None, "NE", "0.00", None, "OEM Outright", "000-110105-92"),
            ],
        )
    yield connection
    connection.close()


def read_csv(path):
    with open(path, encoding="utf-8") as handle:
        return list(csv.reader(handle))


class TestColumnLayout:
    def test_header_matches_the_clients_output_tab(self, conn, tmp_path):
        exporter.to_csv(conn, tmp_path / "out.csv")
        # Taken from the full 109,589 row workbook. The 300 row sample that
        # circulated first has only nine columns and omits Type and
        # Date Processed, which is what this was written against before.
        assert read_csv(tmp_path / "out.csv")[0] == [
            "PART NUMBER",
            "Alternate part number",
            "Description",
            "Condition code",
            "Quantity",
            "Outright price",
            "Sale price",
            "Core value",
            "Type",
            "Price reference",
            "Date Processed",
        ]

    def test_type_is_a_real_column_not_an_extra(self, conn, tmp_path):
        exporter.to_csv(conn, tmp_path / "plain.csv")
        rows = read_csv(tmp_path / "plain.csv")
        assert rows[0][8] == "Type"
        assert rows[1][8] == "OEM Exchange"

    def test_availability_is_opt_in(self, conn, tmp_path):
        exporter.to_csv(conn, tmp_path / "plain.csv")
        exporter.to_csv(conn, tmp_path / "extra.csv", include_extras=True)
        assert "Availability" not in read_csv(tmp_path / "plain.csv")[0]
        assert read_csv(tmp_path / "extra.csv")[0][-1] == "Availability"

    def test_date_processed_is_filled(self, conn, tmp_path):
        exporter.to_csv(conn, tmp_path / "out.csv")
        value = read_csv(tmp_path / "out.csv")[1][10]
        assert len(value) == 10 and value.count("-") == 2

    def test_rows_follow_the_order_of_the_source_sheet(self, conn, tmp_path):
        exporter.to_csv(conn, tmp_path / "out.csv")
        rows = read_csv(tmp_path / "out.csv")[1:]
        assert [row[0] for row in rows] == [
            "9912125-2EX",
            "6622168-22",
            "000-110105-52U/P",
        ]


class TestValues:
    def test_an_exchange_part_carries_its_derived_prices(self, conn, tmp_path):
        exporter.to_csv(conn, tmp_path / "out.csv")
        row = read_csv(tmp_path / "out.csv")[1]
        assert row[3] == "OH"
        assert row[4] == "46"
        assert row[5] == "24224.69"
        assert row[6] == "7724.69"
        assert row[7] == "16500.00"
        assert row[8] == "OEM Exchange"
        assert row[9] == "TXT"

    def test_a_discontinued_part_has_empty_price_cells_not_zeros(self, conn, tmp_path):
        exporter.to_csv(conn, tmp_path / "out.csv")
        row = read_csv(tmp_path / "out.csv")[3]
        assert row[0] == "000-110105-52U/P"
        assert row[4] == ""   # quantity
        assert row[5] == ""   # outright price
        assert row[6] == ""   # sale price
        # A zero here would be read as a real price of nothing.
        assert row[6] != "0"
        assert row[1] == "000-110105-92"  # the replacement part is still useful

    def test_a_missing_alternate_is_blank_not_none(self, conn, tmp_path):
        exporter.to_csv(conn, tmp_path / "out.csv")
        assert read_csv(tmp_path / "out.csv")[2][1] == ""

    def test_a_zero_core_value_is_written_as_zero_not_blank(self, conn, tmp_path):
        # The scope is explicit: a part with no core deposit gets 0 in Core
        # Value. Regression: `_number(...) or ""` blanked it, because
        # Decimal("0") is falsy, and that is most of the list.
        exporter.to_csv(conn, tmp_path / "out.csv")
        row = read_csv(tmp_path / "out.csv")[2]
        assert row[0] == "6622168-22"
        assert row[7] == "0.00"

    def test_a_zero_core_value_survives_the_xlsx_path_too(self, conn, tmp_path):
        from openpyxl import load_workbook

        exporter.to_xlsx(conn, tmp_path / "out.xlsx")
        sheet = load_workbook(tmp_path / "out.xlsx")[exporter.OUTPUT_SHEET]
        assert sheet["H3"].value == 0
        assert sheet["H3"].value is not None


class TestOverwriteSemantics:
    """The sheet is a fixed table that gets overwritten row by row.

    Row N must always mean the same part, so the export is one row per part in
    the order of the client's input tab, carrying the most recent result known
    for that part rather than only the current cycle's.
    """

    def test_every_part_gets_a_row_even_if_never_processed(self, conn, tmp_path):
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO parts(pn, dedupe_key, wh, source_row)"
                " VALUES ('NEVER-SEEN', 'NEVER-SEEN', 'TXT', 4)"
            )
        exporter.to_csv(conn, tmp_path / "out.csv")
        rows = read_csv(tmp_path / "out.csv")[1:]

        assert [r[0] for r in rows][-1] == "NEVER-SEEN"
        assert rows[-1][6] == ""  # no sale price invented for it

    def test_rows_follow_the_input_order_not_the_processing_order(self, conn, tmp_path):
        exporter.to_csv(conn, tmp_path / "out.csv")
        assert [r[0] for r in read_csv(tmp_path / "out.csv")[1:]] == [
            "9912125-2EX",
            "6622168-22",
            "000-110105-52U/P",
        ]

    def test_a_part_not_yet_reached_this_cycle_keeps_its_previous_value(
        self, conn, tmp_path
    ):
        # Otherwise the sheet would go 99% blank on the first day of a new
        # cycle, which would read as the data having been lost.
        with db.transaction(conn):
            conn.execute("INSERT INTO cycles(id) VALUES (2)")
            conn.execute(
                "INSERT INTO results(cycle_id, pn, sale_price, availability)"
                " VALUES (2, '6622168-22', '999.99', 'available')"
            )
        exporter.to_csv(conn, tmp_path / "out.csv")
        rows = {r[0]: r for r in read_csv(tmp_path / "out.csv")[1:]}

        # Refreshed in cycle 2.
        assert rows["6622168-22"][6] == "999.99"
        # Not touched in cycle 2, so cycle 1's value stands.
        assert rows["9912125-2EX"][6] == "7724.69"


class TestTheClientsOwnWorkedExamples:
    """The Output tab of the full workbook ships three filled rows.

    They are the only authoritative statement of what a correct row looks like,
    so the field logic is checked against them directly.

        A                D     G        H       F        I
        6622168-22       NE    357.08      0    357.08   OEM Outright
        00033190-2EX     OH    167.05    500    667.05   OEM Exchange
        01-0790101-04    NE   1332.8    1000   2332.8    OEM Exchange
    """

    @pytest.mark.parametrize(
        "pn,condition,sale,core,outright,part_type",
        [
            ("6622168-22", "NE", "357.08", None, "357.08", "OEM Outright"),
            ("00033190-2EX", "OH", "167.05", "500", "667.05", "OEM Exchange"),
            # The important one: no EX suffix, so condition is NE, yet it
            # carries a core deposit, so the type is still OEM Exchange.
            # Condition code and Type are independent of each other.
            ("01-0790101-04", "NE", "1332.80", "1000", "2332.80", "OEM Exchange"),
        ],
    )
    def test_rules_reproduce_the_example_row(
        self, pn, condition, sale, core, outright, part_type
    ):
        from decimal import Decimal

        from txtav import rules

        sale_d = Decimal(sale)
        core_d = None if core is None else Decimal(core)

        assert rules.condition_code(None, pn).code == condition
        assert rules.outright_price(sale_d, core_d) == Decimal(outright)
        assert rules.core_value(core_d) == Decimal(core or "0")
        assert rules.part_type(core_d) == part_type


class TestReport:
    def test_counts_priced_and_unpriced_rows(self, conn, tmp_path):
        report = exporter.to_csv(conn, tmp_path / "out.csv")
        assert report.rows == 3
        assert report.priced == 2
        assert report.unpriced == 1


class TestXlsx:
    def test_prices_are_written_as_numbers_the_sheet_can_total(self, conn, tmp_path):
        from openpyxl import load_workbook

        exporter.to_xlsx(conn, tmp_path / "out.xlsx")
        sheet = load_workbook(tmp_path / "out.xlsx")[exporter.OUTPUT_SHEET]
        # A spreadsheet holds every number as a double, so the Decimal used
        # for the arithmetic is converted at this boundary and comes back as
        # a float. What matters is that it is a number and not text.
        assert sheet["G2"].value == pytest.approx(7724.69)
        assert not isinstance(sheet["G2"].value, str)
        assert isinstance(sheet["E2"].value, int)

    def test_derived_totals_keep_their_exact_value(self, conn, tmp_path):
        exporter.to_csv(conn, tmp_path / "out.csv")
        # CSV carries the stored Decimal verbatim, so sale + core is exact
        # there: 7724.69 + 16500.00 = 24224.69, no floating point tail.
        row = read_csv(tmp_path / "out.csv")[1]
        assert row[5] == "24224.69"
        assert Decimal(row[6]) + Decimal(row[7]) == Decimal(row[5])

    def test_a_discontinued_row_leaves_price_cells_empty(self, conn, tmp_path):
        from openpyxl import load_workbook

        exporter.to_xlsx(conn, tmp_path / "out.xlsx")
        sheet = load_workbook(tmp_path / "out.xlsx")[exporter.OUTPUT_SHEET]
        assert sheet["G4"].value in (None, "")

    def test_sheet_is_named_output(self, conn, tmp_path):
        from openpyxl import load_workbook

        exporter.to_xlsx(conn, tmp_path / "out.xlsx")
        assert load_workbook(tmp_path / "out.xlsx").sheetnames == ["Output"]
