"""A minimal, exact reader for the client's part list workbook.

openpyxl is not used for this one job. 2,930 of the 109,589 TXT part numbers
are stored in the sheet as numbers rather than text, because whoever built it
let the spreadsheet guess the type. openpyxl hands those back as floats, so
part number 10003 arrives as "10003.0" and 100057022 arrives in scientific
notation; 26 of them additionally carry a date format that openpyxl cannot
coerce and reports as #VALUE!. Every one of those would become a silent
not-found at lookup time.

Reading the sheet XML directly gives the stored value verbatim, which is what
has to go into the portal's search box.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

_M = f"{{{NS_MAIN}}}"
_COL = re.compile(r"([A-Z]+)")


class WorkbookError(ValueError):
    pass


def column_index(cell_ref: str) -> int:
    """A -> 0, B -> 1, AA -> 26."""
    letters = _COL.match(cell_ref)
    if letters is None:
        raise WorkbookError(f"unparseable cell reference {cell_ref!r}")
    index = 0
    for char in letters.group(1):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def render_number(raw: str) -> str:
    """Render a stored numeric cell the way a person reading the sheet sees it.

    Whole numbers lose the trailing .0 and never come back in scientific
    notation, because the result is used as a lookup key.
    """
    value = Decimal(raw)
    if value == value.to_integral_value():
        return str(int(value))
    # Strip the exponent without losing precision, then trim trailing zeros.
    plain = format(value, "f")
    return plain.rstrip("0").rstrip(".") if "." in plain else plain


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    strings: list[str] = []
    with archive.open("xl/sharedStrings.xml") as handle:
        for _, element in ET.iterparse(handle, events=("end",)):
            if element.tag == f"{_M}si":
                # Keep every t run in order; phonetic rPh runs carry no value.
                strings.append("".join(t.text or "" for t in element.iter(f"{_M}t")))
                element.clear()
    return strings


def _sheet_path(archive: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rel_id = None
    available = []
    for sheet in workbook.iter(f"{_M}sheet"):
        available.append(sheet.get("name"))
        if sheet.get("name") == sheet_name:
            rel_id = sheet.get(f"{{{NS_REL}}}id")
    if rel_id is None:
        raise WorkbookError(
            f"workbook has no {sheet_name!r} tab; found {available}"
        )

    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.iter(f"{{{NS_PKG_REL}}}Relationship"):
        if rel.get("Id") == rel_id:
            target = rel.get("Target") or ""
            return target.lstrip("/") if target.startswith("/") else f"xl/{target}"
    raise WorkbookError(f"no relationship target for sheet {sheet_name!r}")


def iter_rows(
    xlsx_path: Path | str, sheet_name: str, max_columns: int = 2
) -> Iterator[tuple[int, list[str]]]:
    """Yield (row_number, [cell values as text]) for every row in the sheet.

    Missing cells are returned as empty strings so callers can index by column
    without worrying about the sparse encoding xlsx uses.
    """
    with zipfile.ZipFile(xlsx_path) as archive:
        shared = _read_shared_strings(archive)
        sheet_path = _sheet_path(archive, sheet_name)

        with archive.open(sheet_path) as handle:
            for _, element in ET.iterparse(handle, events=("end",)):
                if element.tag != f"{_M}row":
                    continue
                row_number = int(element.get("r") or 0)
                values = [""] * max_columns
                for cell in element:
                    try:
                        col = column_index(cell.get("r") or "")
                    except WorkbookError:
                        continue
                    if col >= max_columns:
                        continue
                    values[col] = _cell_text(cell, shared)
                element.clear()
                yield row_number, values


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.get("t")

    if cell_type == "inlineStr":
        is_element = cell.find(f"{_M}is")
        if is_element is None:
            return ""
        return "".join(t.text or "" for t in is_element.iter(f"{_M}t"))

    value = cell.find(f"{_M}v")
    if value is None or value.text is None:
        return ""
    raw = value.text

    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if cell_type in ("str", "b", "e"):
        # str is a formula result, b a boolean, e an in-sheet error such as
        # #VALUE!. All are returned verbatim for the caller to judge.
        return raw
    return render_number(raw)
