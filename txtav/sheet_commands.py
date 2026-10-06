"""CLI commands for the Google Sheet."""

from __future__ import annotations

import argparse

from txtav import db, exporter, sheets
from txtav.config import load_settings


def _require_sheet_id(settings) -> str | None:
    if not settings.sheet_id:
        print(
            "TXTAV_SHEET_ID is not set. It is the id in the sheet's URL, and it "
            "must be a native Google Sheet: the Sheets API cannot write into an "
            "uploaded .xlsx."
        )
        return None
    return settings.sheet_id


def cmd_sheet_auth(args: argparse.Namespace) -> int:
    """One time consent. Opens a browser; a human clicks Allow."""
    settings = load_settings()
    sheet_id = _require_sheet_id(settings)
    if sheet_id is None:
        return 1

    creds = sheets.authorise(settings.google_client_secrets, settings.google_token)
    service = sheets.build_service(creds)
    header = sheets.read_header(service, sheet_id, settings.sheet_tab)

    print(f"Authorised. Token stored at {settings.google_token}")
    print(f"\n{settings.sheet_tab} tab header as the sheet has it:")
    for index, name in enumerate(header):
        print(f"  {chr(ord('A') + index)}  {name}")

    problems = sheets.check_header(service, sheet_id, settings.sheet_tab, exporter.COLUMNS)
    if problems:
        print("\nThis does not match what we write:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("\nColumns match what we write.")
    return 0


def cmd_sheet_push(args: argparse.Namespace) -> int:
    """Overwrite the Output tab from the database."""
    settings = load_settings()
    sheet_id = _require_sheet_id(settings)
    if sheet_id is None:
        return 1

    creds = sheets.authorise(settings.google_client_secrets, settings.google_token)
    service = sheets.build_service(creds)

    # Checked before every write, not just at setup. Writing by position into a
    # reordered sheet would file prices under the wrong heading, and that is the
    # kind of error nobody catches until they have acted on the numbers.
    problems = sheets.check_header(service, sheet_id, settings.sheet_tab, exporter.COLUMNS)
    if problems:
        print("Refusing to write: the sheet's columns are not what we expect.")
        for problem in problems:
            print(f"  {problem}")
        print("\nFix the sheet or update exporter.COLUMNS, then try again.")
        return 2

    conn = db.connect(settings.db_path)
    try:
        rows = exporter.all_rows(conn, processed_only=args.processed_only)
    finally:
        conn.close()

    if not rows:
        print("Nothing to write.")
        return 0

    if args.dry_run:
        print(f"DRY RUN: would write {len(rows):,} rows to {settings.sheet_tab}")
        print("  first row: " + " | ".join(str(v) for v in rows[0]))
        return 0

    # The tab has to be exactly the size of the table. Row N must mean part N,
    # so a short tab would silently truncate and a wide one would waste cells
    # against the spreadsheet's 10 million cell cap.
    if sheets.ensure_size(
        service, sheet_id, settings.sheet_tab, len(rows) + 1, len(exporter.COLUMNS)
    ):
        print(f"Resized {settings.sheet_tab} to {len(rows) + 1:,} rows x {len(exporter.COLUMNS)} columns.")

    cells = sheets.write_rows(service, sheet_id, settings.sheet_tab, rows)
    print(f"Wrote {len(rows):,} rows ({cells:,} cells) to {settings.sheet_tab}.")
    return 0


def register(sub) -> None:
    p_auth = sub.add_parser(
        "sheet-auth", help="authorise once against the Google Sheet"
    )
    p_auth.set_defaults(func=cmd_sheet_auth)

    p_push = sub.add_parser("sheet-push", help="overwrite the Output tab from the store")
    p_push.add_argument("--dry-run", action="store_true")
    p_push.add_argument(
        "--processed-only",
        action="store_true",
        help="write only parts with a result; leaves the tab's row alignment "
        "wrong, so only for inspecting a small sample",
    )
    p_push.set_defaults(func=cmd_sheet_push)
