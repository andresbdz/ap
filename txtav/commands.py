"""CLI commands that drive the browser or report on captured data."""

from __future__ import annotations

import argparse
from pathlib import Path

from txtav import alerts, db, exporter, navigator, runner, supervisor
from txtav.config import load_settings

OUTPUT_COLUMNS = (
    "PART NUMBER",
    "Alternate part number",
    "Description",
    "Condition code",
    "Quantity",
    "Outright price",
    "Sale price",
    "Core value",
    "Price reference",
)


def _run(args: argparse.Namespace, *, dry_run: bool) -> int:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        halt = db.open_halt(conn)
        if halt is not None:
            print(
                f"HALTED: {halt['kind']} raised at {halt['raised_at']}\n"
                f"  {halt['detail']}\n\n"
                f"Look at why this happened before doing anything else. When it has\n"
                f"been dealt with, clear it with:\n"
                f"  txtav halt-clear --id {halt['id']} --by <your name>"
            )
            return 2

        with navigator.browser_session(
            settings.profile_dir, settings.page_timeout_seconds
        ) as page:
            nav = navigator.Navigator(page, settings.base_url)
            if not nav.is_signed_in():
                print(
                    "Not signed in. The session has lapsed.\n"
                    "Run `txtav login`, sign in by hand, then try again."
                )
                return 3
            report = runner.run(
                conn,
                nav,
                settings,
                limit=args.limit,
                dry_run=dry_run,
                # A dry run keeps every page it reads. They are the
                # evidence for the accuracy review, and they fill the gaps
                # in the fixture set, such as a part that is out of stock.
                save_pages=Path(settings.fixtures_dir) / 'dryrun' if dry_run else None,
            )
    finally:
        conn.close()

    print(("DRY RUN, nothing written\n" if dry_run else "") + report.summary())
    return 2 if report.halted is not None else 0


def cmd_run(args: argparse.Namespace) -> int:
    return _run(args, dry_run=False)


def cmd_dryrun(args: argparse.Namespace) -> int:
    return _run(args, dry_run=True)


def cmd_halt_clear(args: argparse.Namespace) -> int:
    """Clear a halt. Deliberately manual: nothing in the code may do this."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        halt = db.open_halt(conn)
        if halt is None:
            print("No open halt.")
            return 0
        if args.id != halt["id"]:
            print(f"Open halt is #{halt['id']}, not #{args.id}. Nothing cleared.")
            return 1
        db.clear_halt(conn, halt["id"], args.by)
        db.log_event(conn, "warning", "halt_cleared", f"#{halt['id']} by {args.by}")
        print(f"Cleared halt #{halt['id']} ({halt['kind']}).")
    finally:
        conn.close()
    return 0


def cmd_results(args: argparse.Namespace) -> int:
    """Print captured rows in the client's Output tab column order."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM results ORDER BY rowid DESC LIMIT ?", (args.limit,)
        ).fetchall()
        if not rows:
            print("No results captured yet.")
            return 0

        print(" | ".join(OUTPUT_COLUMNS))
        print("-" * 110)
        for row in reversed(rows):
            print(
                " | ".join(
                    str(v if v is not None else "")
                    for v in (
                        row["pn"],
                        row["alt_pn"],
                        row["description"],
                        row["condition_code"],
                        row["quantity"],
                        row["outright_price"],
                        row["sale_price"],
                        row["core_value"],
                        row["price_ref"],
                    )
                )
            )
            if row["conflict"]:
                print(f"    needs review: {row['conflict_note']}")
    finally:
        conn.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Write the captured rows out in the client's Output tab layout."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        target = Path(args.out)
        write = exporter.to_csv if target.suffix.lower() == ".csv" else exporter.to_xlsx
        report = write(
            conn,
            target,
            include_extras=args.extras,
            processed_only=args.processed_only,
        )
    finally:
        conn.close()
    print(report.summary())
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    """Daily progress. Also the body of the scheduled daily alert."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        report = alerts.build_daily_report(conn)
    finally:
        conn.close()
    print(report.text())
    if args.send:
        alerts.send(settings.alert_webhook, "TXT parts daily summary", report.text())
    return 2 if report.halt_kind else 0


def cmd_supervise(args: argparse.Namespace) -> int:
    """Run continuously: keep the cycle moving until stopped or halted."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        halt = db.open_halt(conn)
        if halt is not None:
            print(
                f"HALTED: {halt['kind']} raised at {halt['raised_at']}\n"
                f"  {halt['detail']}\n\n"
                f"Deal with the cause, then clear it:\n"
                f"  txtav halt-clear --id {halt['id']} --by <your name>"
            )
            return 2
        report = supervisor.supervise(
            conn,
            settings,
            max_passes=args.max_passes,
            max_hours=args.max_hours,
            pass_parts=args.pass_parts,
        )
    finally:
        conn.close()
    print(report.summary())
    return 2 if report.stopped_because.startswith("halted") else 0


def register(sub) -> None:
    p_dry = sub.add_parser("dryrun", help="fetch a few parts without writing results")
    p_dry.add_argument("--limit", type=int, default=5)
    p_dry.set_defaults(func=cmd_dryrun)

    p_run = sub.add_parser("run", help="process pending parts")
    p_run.add_argument("--limit", type=int, default=None)
    p_run.set_defaults(func=cmd_run)

    p_clear = sub.add_parser("halt-clear", help="clear a halt after investigating it")
    p_clear.add_argument("--id", type=int, required=True)
    p_clear.add_argument("--by", required=True, help="who checked and cleared it")
    p_clear.set_defaults(func=cmd_halt_clear)

    p_exp = sub.add_parser("export", help="write results in the Output tab layout")
    p_exp.add_argument("--out", default="data/Output.xlsx", help=".xlsx or .csv")
    p_exp.add_argument(
        "--extras", action="store_true", help="also write Availability"
    )
    p_exp.add_argument(
        "--processed-only",
        action="store_true",
        help="skip parts with no result yet, for sharing a sample",
    )
    p_exp.set_defaults(func=cmd_export)

    p_sum = sub.add_parser("summary", help="daily progress report")
    p_sum.add_argument("--send", action="store_true", help="also post to the webhook")
    p_sum.set_defaults(func=cmd_summary)

    p_sup = sub.add_parser(
        "supervise", help="run continuously until stopped or halted"
    )
    p_sup.add_argument("--max-passes", type=int, default=None)
    p_sup.add_argument("--max-hours", type=float, default=None)
    p_sup.add_argument(
        "--pass-parts",
        type=int,
        default=supervisor.PASS_PARTS,
        help="parts per pass; the loop only rechecks limits, halts and the "
        "session between passes",
    )
    p_sup.set_defaults(func=cmd_supervise)

    p_res = sub.add_parser("results", help="print captured rows")
    p_res.add_argument("--limit", type=int, default=20)
    p_res.set_defaults(func=cmd_results)
