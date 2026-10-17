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

    if args.processed_only:
        # This used to be allowed, and it was a loaded gun. Skipping unprocessed
        # parts makes the rows no longer line up with the tab, and the resize
        # below would then shrink a 109,589 row table to however many have been
        # done so far. That is precisely the misalignment that took a day to
        # find and repair on 2026-10-06, reachable from a documented flag.
        print(
            "sheet-push cannot write a partial table: row N of the Output tab has\n"
            "to keep meaning part N, and skipping unprocessed parts would shift\n"
            "every row and shrink the tab to the number done so far.\n\n"
            "A full push already leaves unprocessed parts carrying their previous\n"
            "value, so it is safe at any point in a cycle:\n"
            "  txtav sheet-push\n\n"
            "For a sample to look at or send, export a file instead:\n"
            "  txtav export --processed-only --out data/sample.xlsx"
        )
        return 1

    creds = sheets.authorise(settings.google_client_secrets, settings.google_token)
    service = sheets.build_service(creds)

    # Checked before every write, not just at setup. Writing by position into a
    # reordered sheet would file prices under the wrong heading, and that is the
    # kind of error nobody catches until they have acted on the numbers.
    #
    # --repair-header is the way out when the header itself is wrong: a full
    # push rebuilds the whole tab from the database, so it should be able to
    # rebuild row 1 too rather than refusing and leaving no way back.
    problems = sheets.check_header(service, sheet_id, settings.sheet_tab, exporter.COLUMNS)
    if problems and not args.repair_header:
        print("Refusing to write: the sheet's columns are not what we expect.")
        for problem in problems:
            print(f"  {problem}")
        print(
            "\nFix the sheet, or update exporter.COLUMNS, or rebuild the whole tab\n"
            "from the database with:  txtav sheet-push --repair-header"
        )
        return 2

    conn = db.connect(settings.db_path)
    try:
        rows = exporter.all_rows(conn)
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

    if args.repair_header:
        sheets.write_header(service, sheet_id, settings.sheet_tab, exporter.COLUMNS)
        print("Rewrote the header row.")

    cells = sheets.write_rows(service, sheet_id, settings.sheet_tab, rows)
    print(f"Wrote {len(rows):,} rows ({cells:,} cells) to {settings.sheet_tab}.")
    return 0


def cmd_review_push(args: argparse.Namespace) -> int:
    """Rewrite the Review tab: every flagged part and why."""
    settings = load_settings()
    sheet_id = _require_sheet_id(settings)
    if sheet_id is None:
        return 1
    conn = db.connect(settings.db_path)
    try:
        rows = exporter.review_rows(conn)
    finally:
        conn.close()
    service = sheets.build_service(
        sheets.authorise(settings.google_client_secrets, settings.google_token)
    )
    sheets.replace_tab(service, sheet_id, exporter.REVIEW_TAB, exporter.REVIEW_COLUMNS, rows)
    print(f"Wrote {len(rows):,} flagged parts to the {exporter.REVIEW_TAB} tab.")
    return 0


def register(sub) -> None:
    p_auth = sub.add_parser(
        "sheet-auth", help="authorise once against the Google Sheet"
    )
    p_auth.set_defaults(func=cmd_sheet_auth)

    p_push = sub.add_parser("sheet-push", help="overwrite the Output tab from the store")
    p_push.add_argument("--dry-run", action="store_true")
    p_push.add_argument(
        "--repair-header",
        action="store_true",
        help="also rewrite row 1, for when the header itself has been damaged",
    )
    p_push.add_argument(
        "--processed-only",
        action="store_true",
        help="refused: it would shift every row. Use `txtav export "
        "--processed-only` for a sample instead",
    )
    p_push.set_defaults(func=cmd_sheet_push)

    p_review = sub.add_parser(
        "review-push", help="rewrite the Review tab with every flagged part and why"
    )
    p_review.set_defaults(func=cmd_review_push)
