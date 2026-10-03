"""Load the client's part list into the job store.

The workbook has two tabs. "Parts to lookup and where" holds the input:
column A is the part number, column B is the site it belongs to. Only rows
marked TXT are in scope for this phase. The "Output" tab is where results go
and is not read here.

The full sheet is 109,789 rows of which 109,589 are TXT, so the read is done
in openpyxl's read only mode to keep memory flat.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from txtav import db, urls, xlsx_reader

INPUT_SHEET = "Parts to lookup and where"
OUTPUT_SHEET = "Output"
TXT_MARKER = "TXT"

# Values a spreadsheet puts in a cell when a formula or a format failed. These
# are not part numbers and must not be looked up.
SHEET_ERRORS = frozenset(
    {"#VALUE!", "#REF!", "#N/A", "#NAME?", "#DIV/0!", "#NULL!", "#NUM!"}
)


@dataclass
class ImportReport:
    total_rows: int = 0
    txt_rows: int = 0
    other_wh: dict[str, int] = field(default_factory=dict)
    imported: int = 0
    duplicates: list[tuple[str, int]] = field(default_factory=list)
    blank_pn_rows: list[int] = field(default_factory=list)
    error_cells: list[tuple[str, int]] = field(default_factory=list)
    path_hostile: int = 0

    def summary(self) -> str:
        lines = [
            f"rows read:        {self.total_rows}",
            f"TXT rows:         {self.txt_rows}",
            f"imported:         {self.imported}",
            f"duplicates:       {len(self.duplicates)}",
            f"blank part no:    {len(self.blank_pn_rows)}",
            f"sheet errors:     {len(self.error_cells)}",
            f"need search box:  {self.path_hostile}",
        ]
        if self.other_wh:
            others = ", ".join(f"{k}={v}" for k, v in sorted(self.other_wh.items()))
            lines.append(f"skipped (not TXT): {others}")
        return "\n".join(lines)


def read_rows(xlsx_path: Path | str) -> list[tuple[int, str, str]]:
    """Return (source_row, part_number, wh) for every data row in the input tab."""
    rows: list[tuple[int, str, str]] = []
    for row_number, values in xlsx_reader.iter_rows(xlsx_path, INPUT_SHEET, max_columns=2):
        if row_number == 1:
            continue  # header: PART NUMBER | WH
        pn, wh = values[0], values[1].strip()
        if not pn.strip() and not wh:
            continue  # trailing blank row
        rows.append((row_number, pn, wh))
    return rows


def import_parts(conn: sqlite3.Connection, xlsx_path: Path | str) -> ImportReport:
    """Replace the parts table from the workbook, reporting what was found.

    Duplicates are detected case insensitively and only the first occurrence is
    kept, so a part is never looked up twice in one cycle.
    """
    report = ImportReport()
    seen: dict[str, int] = {}
    to_insert: list[tuple[str, str, str, int, int]] = []

    for source_row, raw_pn, wh in read_rows(xlsx_path):
        report.total_rows += 1

        if wh.upper() != TXT_MARKER:
            report.other_wh[wh or "(blank)"] = report.other_wh.get(wh or "(blank)", 0) + 1
            continue

        report.txt_rows += 1
        pn = urls.normalise_pn(raw_pn)
        if not pn:
            report.blank_pn_rows.append(source_row)
            continue
        if pn.upper() in SHEET_ERRORS:
            # The source sheet itself failed to produce a value here. Looking
            # it up would waste a request and return a meaningless not-found.
            report.error_cells.append((pn, source_row))
            continue

        key = urls.dedupe_key(pn)
        if key in seen:
            report.duplicates.append((pn, source_row))
            continue
        seen[key] = source_row

        hostile = urls.needs_search_box(pn)
        if hostile:
            report.path_hostile += 1
        to_insert.append((pn, key, wh.upper(), source_row, int(hostile)))

    with db.transaction(conn):
        conn.execute("DELETE FROM parts")
        conn.executemany(
            "INSERT INTO parts(pn, dedupe_key, wh, source_row, path_hostile) "
            "VALUES (?, ?, ?, ?, ?)",
            to_insert,
        )
    report.imported = len(to_insert)

    db.log_event(
        conn,
        "info",
        "import",
        f"imported {report.imported} TXT parts from {Path(xlsx_path).name}, "
        f"{len(report.duplicates)} duplicates skipped, "
        f"{len(report.error_cells)} sheet error cells skipped, "
        f"{report.path_hostile} need the search box",
    )
    return report
