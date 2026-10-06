"""Command line entry point.

`import`, `status`, `results` and `halt-clear` are offline. `login` opens a
browser for a human to sign in by hand. `dryrun` and `run` are the only
commands that look parts up on the portal, and both refuse to start while a
halt is open or the session has lapsed.
"""

from __future__ import annotations

import argparse
import re
import sys

from txtav import commands, db, importer, logging_setup, sheet_commands
from txtav.config import load_settings


def cmd_import(args: argparse.Namespace) -> int:
    settings = load_settings()
    path = args.xlsx or settings.parts_xlsx
    conn = db.connect(settings.db_path)
    try:
        report = importer.import_parts(conn, path)
    finally:
        conn.close()
    print(f"source: {path}")
    print(report.summary())
    if report.duplicates:
        print("\nfirst duplicates skipped:")
        for pn, row in report.duplicates[:10]:
            print(f"  row {row}: {pn}")
    if report.blank_pn_rows:
        print(f"\nblank part number rows: {report.blank_pn_rows[:10]}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        parts = conn.execute("SELECT COUNT(*) c FROM parts").fetchone()["c"]
        hostile = conn.execute(
            "SELECT COUNT(*) c FROM parts WHERE path_hostile = 1"
        ).fetchone()["c"]
        halt = db.open_halt(conn)

        print(f"database:        {settings.db_path}")
        print(f"parts loaded:    {parts}")
        print(f"need search box: {hostile}")

        if parts:
            interval = settings.request_interval_seconds
            per_day = parts / settings.cycle_days
            print(
                f"\npacing at {interval:.1f}s/part over {settings.cycle_days} days:"
                f"\n  required: {per_day:,.0f}/day"
                f" = {per_day / 24:,.1f}/hour"
                f" = {per_day / 24 / 60:.2f}/min"
                f"\n  implied interval: {settings.cycle_days * 24 * 3600 / parts:.1f}s"
            )

        cycles = conn.execute(
            "SELECT id, status, started_at FROM cycles ORDER BY id DESC LIMIT 3"
        ).fetchall()
        if cycles:
            print("\nrecent cycles:")
            for row in cycles:
                print(f"  #{row['id']} {row['status']} started {row['started_at']}")

        if halt is not None:
            print(
                f"\nHALTED: {halt['kind']} raised at {halt['raised_at']}"
                f"\n  {halt['detail']}"
                f"\n  runs are blocked until this is cleared by an operator"
            )
    finally:
        conn.close()
    return 0


LOGGED_OUT_MARKERS = ("hello, log in", "my account log in register")


def cmd_login(args: argparse.Namespace) -> int:
    """Open a real browser so a human can sign in, then verify the session.

    The sign in and any 2FA are done by hand in this window. Nothing is
    automated and no credential passes through this process; the resulting
    session lives in the Playwright profile directory and is reused by every
    later run.

    Always start the browser this way. The profile is tied to one Chromium
    build, and opening it with a different Playwright version drops the
    session cookies, which is what happened on 2026-10-02.
    """
    from playwright.sync_api import sync_playwright

    settings = load_settings()
    dashboard = f"{settings.base_url.rstrip('/')}/Dashboard/"

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(settings.profile_dir), headless=False, viewport=None
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(settings.page_timeout_seconds * 1000)
        page.goto(dashboard, wait_until="domcontentloaded")

        print(f"profile: {settings.profile_dir}")
        print(f"opened:  {dashboard}")
        print("\nSign in by hand in the browser window, including the 2FA code.")
        input("Press Enter here once you are signed in...")

        page.goto(dashboard, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        body = re.sub(r"\s+", " ", page.inner_text("body")).lower()
        logged_out = any(marker in body for marker in LOGGED_OUT_MARKERS)
        current = page.url
        context.close()

    if logged_out:
        print(f"\nNOT signed in. The page at {current} still offers a log in link.")
        return 1
    print("\nSigned in. The session is stored in the profile and will be reused.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="txtav", description=__doc__)
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="load the part list into the job store")
    p_import.add_argument("--xlsx", help="workbook path (defaults to TXTAV_PARTS_XLSX)")
    p_import.set_defaults(func=cmd_import)

    p_login = sub.add_parser("login", help="sign in by hand and store the session")
    p_login.set_defaults(func=cmd_login)

    p_status = sub.add_parser("status", help="report on the job store")
    p_status.set_defaults(func=cmd_status)

    commands.register(sub)
    sheet_commands.register(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    logging_setup.configure(settings.log_dir, args.log_level)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
