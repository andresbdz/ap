"""Writing results into the client's Google Sheet.

Authenticated as a person, not a service account: the client's Google
organization enforces `iam.managed.disableServiceAccountKeyCreation`, so no
service account key can be issued. The OAuth app is Internal, which keeps it
inside their organization and, unlike an External app left in testing, does not
expire its refresh token after seven days.

The sheet is a fixed table of 109,589 rows plus a header, overwritten in place:
row N always means the same part. Writes go out in blocks rather than row by
row, because one request per part would be 109,589 API calls per cycle against
a quota measured in hundreds per minute.
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from txtav import logging_setup

log = logging_setup.get_logger("txtav.sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Rows per API call. The Sheets API allows 60 write requests per minute per
# user, and 1,000 row blocks meant 110 requests for a full table, which ran
# into that at around row 61,000. 10,000 rows is 11 requests for the whole
# 109,589, comfortably inside both the rate limit and the request size limit.
BLOCK_ROWS = 10_000

# Sheets allows 60 writes per minute per user. Retrying a rejected write
# immediately just burns more of the same budget, so a 429 waits for the
# window to roll over rather than backing off in small steps.
RATE_LIMIT_WAIT_SECONDS = 62
MAX_RATE_LIMIT_RETRIES = 3


class SheetError(RuntimeError):
    pass


def authorise(client_secrets: Path, token_path: Path, *, open_browser: bool = True):
    """Return credentials, running the consent flow once if needed.

    The refresh token is written to `token_path` and reused from then on. A
    human only sees a browser the first time, or if the token is revoked.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(token_path, creds)
            return creds
        except Exception as exc:  # noqa: BLE001
            log.warning("token_refresh_failed", error=f"{type(exc).__name__}: {exc}")

    if not client_secrets.exists():
        raise SheetError(
            f"{client_secrets} is missing. It is the OAuth client JSON from the "
            f"Google Cloud project, and it is deliberately not in the repository."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=open_browser)
    _save_token(token_path, creds)
    return creds


def _save_token(token_path: Path, creds) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    log.info("token_saved", path=str(token_path))


def build_service(creds):
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_header(service, sheet_id: str, tab: str) -> list[str]:
    """The tab's first row, used to check our columns still line up with theirs."""
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{tab}!1:1")
        .execute()
    )
    values = result.get("values", [])
    return values[0] if values else []


def check_header(service, sheet_id: str, tab: str, expected: Sequence[str]) -> list[str]:
    """Return the differences between the sheet's header and ours.

    Called before every write. If the client reorders or renames a column,
    writing by position would put prices under the wrong heading, and nobody
    would notice until someone acted on the numbers.
    """
    actual = read_header(service, sheet_id, tab)
    problems = []
    if len(actual) < len(expected):
        problems.append(
            f"sheet has {len(actual)} columns, expected at least {len(expected)}"
        )
    for index, name in enumerate(expected):
        found = actual[index] if index < len(actual) else ""
        if found.strip().lower() != name.strip().lower():
            column = chr(ord("A") + index)
            problems.append(f"column {column}: sheet says {found!r}, expected {name!r}")
    return problems


def is_rate_limited(exc: Exception) -> bool:
    """True for the Sheets API's 'too many writes this minute' response."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status == 429 or "RATE_LIMIT_EXCEEDED" in str(exc)


def execute_with_retry(request, *, sleep=time.sleep):
    """Run an API request, waiting out the quota window if it is exhausted.

    Only rate limiting is retried. Everything else propagates: a 403 or a bad
    range is a real problem and quietly trying again would hide it.
    """
    for attempt in range(1, MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return request.execute()
        except Exception as exc:  # noqa: BLE001 - re-raised unless rate limited
            if not is_rate_limited(exc) or attempt == MAX_RATE_LIMIT_RETRIES:
                raise
            log.warning(
                "sheet_rate_limited",
                attempt=attempt,
                waiting_seconds=RATE_LIMIT_WAIT_SECONDS,
            )
            sleep(RATE_LIMIT_WAIT_SECONDS)
    raise SheetError("unreachable")


def column_letter(count: int) -> str:
    """Spreadsheet letter for a 1-based column number. 1 -> A, 11 -> K, 27 -> AA.

    The range has to name the last column we actually write. Sending eleven
    columns into a range of "A2:A1001" is rejected outright, which is the
    better outcome: a range that was too wide would scribble over whatever sat
    beyond the table.
    """
    if count < 1:
        raise ValueError("column count must be at least 1")
    letters = ""
    while count:
        count, remainder = divmod(count - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _cell(value: object) -> object:
    """Convert one value for the JSON request body.

    Decimal is used for the arithmetic, where it matters, and converted here at
    the boundary: the API body is JSON, which has no decimal type, and a
    spreadsheet stores every number as a double anyway. Explicit so it is not
    mistaken for an accident, and so a future type lands here rather than in a
    TypeError halfway through writing 109,589 rows.
    """
    if isinstance(value, Decimal):
        return float(value)
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def tab_id(service, sheet_id: str, tab: str) -> int:
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for sheet in meta.get("sheets", []):
        properties = sheet["properties"]
        if properties["title"] == tab:
            return int(properties["sheetId"])
    raise SheetError(f"no tab named {tab!r} in the spreadsheet")


def ensure_size(service, sheet_id: str, tab: str, rows: int, columns: int) -> bool:
    """Resize the tab to exactly fit the table. Returns True if it changed.

    The Output tab ships at 1,000 rows by 32 columns and the table needs
    109,590 by 11. Trimming the unused columns matters as well as adding rows:
    a spreadsheet is capped at 10 million cells in total, and leaving 21 empty
    columns on a 109,590 row tab would spend 2.3 million of them on nothing.
    """
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    properties = None
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["title"] == tab:
            properties = sheet["properties"]
            break
    if properties is None:
        raise SheetError(f"no tab named {tab!r} in the spreadsheet")

    grid = properties["gridProperties"]
    if grid["rowCount"] == rows and grid["columnCount"] == columns:
        return False

    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": properties["sheetId"],
                            "gridProperties": {
                                "rowCount": rows,
                                "columnCount": columns,
                            },
                        },
                        "fields": "gridProperties.rowCount,gridProperties.columnCount",
                    }
                }
            ]
        },
    ).execute()
    log.info(
        "sheet_resized",
        tab=tab,
        rows_from=grid["rowCount"],
        rows_to=rows,
        cols_from=grid["columnCount"],
        cols_to=columns,
    )
    return True


def write_range(
    service,
    sheet_id: str,
    tab: str,
    rows: Sequence[Sequence[object]],
    first_row: int,
) -> int:
    """Write a contiguous block of rows at a known position.

    Parts are processed in the order of the client's input tab, so the rows
    that finish during a run are always contiguous. That makes a live update
    one request for a few hundred rows rather than a rewrite of the whole
    109,589 row table.
    """
    if not rows:
        return 0
    width = max(len(row) for row in rows)
    last = first_row + len(rows) - 1
    body = {"values": [[_cell(value) for value in row] for row in rows]}
    result = execute_with_retry(
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=sheet_id,
            range=f"{tab}!A{first_row}:{column_letter(width)}{last}",
            valueInputOption="USER_ENTERED",
            body=body,
        )
    )
    cells = int(result.get("updatedCells", 0))
    log.info("sheet_range_written", first_row=first_row, last_row=last, cells=cells)
    return cells


def write_rows(
    service,
    sheet_id: str,
    tab: str,
    rows: Sequence[Sequence[object]],
    *,
    first_data_row: int = 2,
    block_rows: int = BLOCK_ROWS,
) -> int:
    """Overwrite the data rows in place. Returns the number of cells updated.

    USER_ENTERED so numbers land as numbers and the client can total a column.
    Values are sent as given: the exporter has already decided that a part with
    no price gets an empty cell rather than a zero.
    """
    width = max((len(row) for row in rows), default=0)
    last_column = column_letter(width)

    updated = 0
    for offset in range(0, len(rows), block_rows):
        block = rows[offset : offset + block_rows]
        start = first_data_row + offset
        end = start + len(block) - 1
        body = {"values": [[_cell(value) for value in row] for row in block]}
        result = execute_with_retry(
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=sheet_id,
                range=f"{tab}!A{start}:{last_column}{end}",
                valueInputOption="USER_ENTERED",
                body=body,
            )
        )
        updated += int(result.get("updatedCells", 0))
        log.info("sheet_block_written", start=start, end=end, rows=len(block))
    return updated
