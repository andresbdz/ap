"""Write captured results out in the client's Output tab layout.

Column order is taken from the Output tab of the client's own workbook and must
not be rearranged: they paste or import this straight into their sheet.

    A  PART NUMBER            G  Sale price
    B  Alternate part number  H  Core value
    C  Description            I  Type
    D  Condition code         J  Price reference
    E  Quantity               K  Date Processed
    F  Outright price

Read from the full 109,589 row workbook, not the 300 row sample that was
circulated first: the sample's Output tab has only nine columns and omits Type
and Date Processed.

Prices are written as numbers so the sheet can total them, not as the strings
they are stored as. A part with no price, which is what a discontinued part
looks like, gets an empty cell rather than a zero: a zero in a pricing sheet
reads as a real price of nothing.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

OUTPUT_SHEET = "Output"

COLUMNS = (
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
)

# Not in the client's sheet. Written only on request, because it is the one
# piece of information their layout has nowhere to put: whether a blank price
# means discontinued, sold on quotation, or something unexpected.
EXTRA_COLUMNS = ("Availability",)

# One row per part, in the order of the client's input tab, carrying the most
# recent result known for that part.
#
# Rows are overwritten in place rather than appended, so the sheet is a fixed
# 109,589 row table and row N must always mean the same part. Two consequences:
# a part not yet reached in the current cycle keeps the value from the previous
# one rather than going blank, and a part never processed still occupies its
# row with only its part number filled in.
SELECT = """
SELECT p.pn,
       r.alt_pn, r.description, r.condition_code, r.quantity,
       r.outright_price, r.sale_price, r.core_value, r.part_type,
       r.price_ref, date(r.scraped_at) AS processed_on, r.availability
FROM parts p
LEFT JOIN results r
  ON r.pn = p.pn
 AND r.cycle_id = (SELECT MAX(cycle_id) FROM results x WHERE x.pn = p.pn)
ORDER BY p.source_row
"""


@dataclass
class ExportReport:
    rows: int
    path: Path
    priced: int
    unpriced: int

    def summary(self) -> str:
        return (
            f"wrote {self.rows} rows to {self.path}\n"
            f"  with a price:    {self.priced}\n"
            f"  without a price: {self.unpriced}"
        )


def _number(value: str | None) -> Decimal | None:
    """Stored money is text to keep its precision. Export it as a number."""
    if value is None or value == "":
        return None
    return Decimal(value)


def _cell(value: Decimal | None) -> Decimal | str:
    """A money cell: the number, or empty when there is genuinely no value.

    Written as an explicit None check rather than `value or ""`, because
    Decimal("0") is falsy. The scope requires Core Value to be 0 when a part
    has no core deposit, and the truthiness version silently blanked that
    column on every outright part, which is most of the list.
    """
    return "" if value is None else value


def all_rows(
    conn: sqlite3.Connection,
    *,
    include_extras: bool = False,
    processed_only: bool = False,
) -> list[list[object]]:
    """Every part in input order, or only the ones with a result yet.

    `processed_only` is for sharing a sample or spot checking. The sheet itself
    always wants the full table, so row N keeps meaning the same part.
    """
    out: list[list[object]] = []
    for row in conn.execute(SELECT):
        if processed_only and row["processed_on"] is None:
            continue
        out.append(_row_values(row, include_extras=include_extras))
    return out


def _row_values(row, *, include_extras: bool) -> list[object]:
    """One database row as the client's column order. The single place that
    decides what goes in which column; everything else writes what this says."""
    record: list[object] = [
        row["pn"],
        row["alt_pn"] or "",
        row["description"] or "",
        row["condition_code"] or "",
        row["quantity"] if row["quantity"] is not None else "",
        _cell(_number(row["outright_price"])),
        _cell(_number(row["sale_price"])),
        _cell(_number(row["core_value"])),
        row["part_type"] or "",
        row["price_ref"] or "",
        row["processed_on"] or "",
    ]
    if include_extras:
        record.append(row["availability"] or "")
    return record


RANGE_SELECT = SELECT.replace(
    "ORDER BY p.source_row", "WHERE p.source_row BETWEEN ? AND ? ORDER BY p.source_row"
)


def rows_in_range(
    conn: sqlite3.Connection, first_source_row: int, last_source_row: int
) -> list[list[object]]:
    """Rows for a contiguous slice of the input tab, in that tab's order.

    Used for live updates during a run. Parts are processed in source_row
    order, so what finishes is always a contiguous block and can go to the
    sheet as one request rather than a rewrite of the whole table.
    """
    out: list[list[object]] = []
    for row in conn.execute(RANGE_SELECT, (first_source_row, last_source_row)):
        out.append(_row_values(row, include_extras=False))
    return out


def latest_cycle(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM cycles ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else int(row["id"])


def to_csv(
    conn: sqlite3.Connection,
    path: Path | str,
    *,
    include_extras: bool = False,
    processed_only: bool = False,
) -> ExportReport:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    header = list(COLUMNS) + (list(EXTRA_COLUMNS) if include_extras else [])
    rows = all_rows(conn, include_extras=include_extras, processed_only=processed_only)

    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])

    priced = sum(1 for row in rows if row[6] != "")
    return ExportReport(len(rows), target, priced, len(rows) - priced)


def to_xlsx(
    conn: sqlite3.Connection,
    path: Path | str,
    *,
    include_extras: bool = False,
    processed_only: bool = False,
) -> ExportReport:
    """Write a real workbook, so prices arrive as numbers and not as text."""
    from openpyxl import Workbook

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    header = list(COLUMNS) + (list(EXTRA_COLUMNS) if include_extras else [])
    rows = all_rows(conn, include_extras=include_extras, processed_only=processed_only)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = OUTPUT_SHEET
    sheet.append(header)
    for row in rows:
        # Decimal is used for the arithmetic, where it matters, and converted
        # here at the boundary. A spreadsheet stores every number as a double
        # anyway, so writing a float is what the client's sheet would hold
        # regardless; the conversion is explicit so it is not mistaken for an
        # accident.
        sheet.append(
            [
                ""
                if value is None
                else float(value)
                if isinstance(value, Decimal)
                else value
                for value in row
            ]
        )

    for column in ("F", "G", "H"):
        for cell in sheet[column][1:]:
            cell.number_format = "#,##0.00"

    workbook.save(target)
    priced = sum(1 for row in rows if row[6] != "")
    return ExportReport(len(rows), target, priced, len(rows) - priced)
