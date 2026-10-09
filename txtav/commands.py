"""CLI commands that drive the browser or report on captured data."""

from __future__ import annotations

import argparse
import functools
import sqlite3
from pathlib import Path

from txtav import alerts, db, exporter, instance_lock, navigator, runner, supervisor
from txtav.config import load_settings

# Taken from the exporter rather than restated. This was a second copy of the
# column order and it had already drifted: it still listed the nine columns of
# the sample workbook, without Type or Date Processed, so `txtav results`
# printed a layout the sheet no longer has.
OUTPUT_COLUMNS = exporter.BASE_COLUMNS  # the tracking dates come from more than one table


def holds_portal_lock(command):
    """Run a command only while holding the one-process-at-a-time lock.

    For every command that opens the portal session: login, run, dryrun and
    supervise. The lock lives next to the database, so it covers exactly the
    processes that would share a job store and a browser profile.
    """

    @functools.wraps(command)
    def guarded(args: argparse.Namespace) -> int:
        settings = load_settings()
        lock = instance_lock.InstanceLock(Path(settings.db_path).with_suffix(".lock"))
        try:
            lock.acquire()
        except instance_lock.AlreadyRunning as exc:
            print(f"Not started: {exc}\nStop the other one first, or wait for it to finish.")
            return 1
        try:
            return command(args)
        finally:
            lock.release()

    return guarded


def _refuse_empty_store(settings) -> bool:
    """True, after explaining, when there is nothing loaded to look up.

    Every path in the settings is relative to where the command is started.
    From the wrong directory, connect() creates a fresh empty database, a cycle
    of nothing opens and finishes at once, and a supervisor then idles for two
    weeks while reporting nothing wrong. On a machine reached by remote desktop,
    a terminal opened in the home directory is the likely way to get there.

    Checked read only and before connecting, so the check does not itself leave
    an empty database behind in the wrong place.
    """
    path = Path(settings.db_path)
    if path.exists():
        probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            loaded = probe.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
        except sqlite3.DatabaseError:
            loaded = 0
        finally:
            probe.close()
        if loaded:
            return False
    print(
        f"No parts loaded in {Path(settings.db_path).resolve()}.\n"
        f"Either this is not the project directory (it is {Path.cwd()}),\n"
        f"or the list has not been imported: `txtav import`."
    )
    return True


def _run(args: argparse.Namespace, *, dry_run: bool) -> int:
    settings = load_settings()
    if _refuse_empty_store(settings):
        return 1
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


@holds_portal_lock
def cmd_run(args: argparse.Namespace) -> int:
    return _run(args, dry_run=False)


@holds_portal_lock
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
                        row["part_type"],
                        row["price_ref"],
                        (row["scraped_at"] or "")[:10],
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


def cmd_cycle_restart(args: argparse.Namespace) -> int:
    """Move the running cycle's window to start now. Results are kept."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT id, started_at, ends_at FROM cycles WHERE status = 'running'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            print("No running cycle. The next `run` or `supervise` opens one.")
            return 0
        progress = runner.cycle_progress(conn, int(row["id"]))
        print(f"cycle {row['id']}: {row['started_at']} to {row['ends_at']} (UTC)")
        if progress is not None:
            print(f"  {progress.describe()}")
        if not args.yes:
            print(
                f"\nWould restart its window: from now, for {settings.cycle_days} days.\n"
                f"Every result already captured is kept and nothing is looked up again.\n"
                f"Run again with --yes to do it."
            )
            return 1
        started, ends = runner.restart_cycle_window(
            conn, int(row["id"]), settings.cycle_days
        )
        print(f"\nRestarted: cycle {row['id']} now runs {started} to {ends} (UTC).")
    finally:
        conn.close()
    return 0


@holds_portal_lock
def cmd_supervise(args: argparse.Namespace) -> int:
    """Run continuously: keep the cycle moving until stopped or halted."""
    settings = load_settings()
    if _refuse_empty_store(settings):
        return 1
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
            session_passes=args.session_passes,
        )
    finally:
        conn.close()
    print(report.summary())
    # Non zero for every stop that needs a person, so a wrapper script or a
    # scheduled restart can tell "finished" from "come and look".
    needs_a_person = (
        "halted",
        "crashed",
        "session expired",
        "not signed in",
        "portal unreachable",
        "already running",
    )
    return 2 if report.stopped_because.startswith(needs_a_person) else 0


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
    p_sup.add_argument(
        "--session-passes",
        type=int,
        default=supervisor.SESSION_PASSES,
        help="passes before the browser is closed and reopened; the session "
        "lives in the profile, so this costs a couple of seconds and bounds "
        "how long one Chromium tab has to stay healthy",
    )
    p_sup.set_defaults(func=cmd_supervise)

    p_cyc = sub.add_parser(
        "cycle-restart",
        help="start the running cycle's window now, keeping its results",
    )
    p_cyc.add_argument("--yes", action="store_true", help="actually do it")
    p_cyc.set_defaults(func=cmd_cycle_restart)

    p_res = sub.add_parser("results", help="print captured rows")
    p_res.add_argument("--limit", type=int, default=20)
    p_res.set_defaults(func=cmd_results)
