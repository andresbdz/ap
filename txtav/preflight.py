"""Checks to run on a machine before trusting it with a fourteen day run.

The run goes unattended on a machine reached only by remote desktop, so what it
needs has to be confirmed before it starts rather than discovered missing in the
middle of the night. Each check says PASS, WARN or FAIL and what to do about it.
FAIL means the run cannot work; WARN means it can, but something a person relies
on, such as being told about a halt, is missing.

Nothing here contacts the portal. The session is confirmed by `txtav login`,
which a person runs on that machine anyway. The only network call is to Google,
to prove the sheet can be written without anyone at the keyboard.
"""

from __future__ import annotations

import importlib
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

MIN_PYTHON = (3, 12)
MIN_FREE_GB = 2.0

# Imported lazily by the code that uses them, which is exactly why their absence
# would otherwise only show up mid run.
RUNTIME_PACKAGES = (
    ("playwright", "playwright"),
    ("lxml", "lxml"),
    ("googleapiclient", "google-api-python-client"),
    ("google_auth_oauthlib", "google-auth-oauthlib"),
    ("openpyxl", "openpyxl"),
)


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str

    def line(self) -> str:
        return f"[{self.status}] {self.name}: {self.detail}"


def check_python(version: tuple[int, ...] = tuple(sys.version_info[:3])) -> Check:
    shown = ".".join(str(part) for part in version)
    if tuple(version[:2]) >= MIN_PYTHON:
        return Check(PASS, "python", shown)
    wanted = ".".join(str(part) for part in MIN_PYTHON)
    return Check(FAIL, "python", f"{shown}, needs {wanted} or later")


def check_packages(importer=importlib.import_module) -> Check:
    missing = []
    for module, package in RUNTIME_PACKAGES:
        try:
            importer(module)
        except ImportError:
            missing.append(package)
    if not missing:
        return Check(PASS, "packages", "all runtime dependencies import")
    return Check(
        FAIL,
        "packages",
        f"missing {', '.join(missing)}. Install with: uv pip install -e .",
    )


def check_browser() -> Check:
    """Playwright's own Chromium, which `pip install` does not download."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            executable = Path(p.chromium.executable_path)
    except Exception as exc:  # noqa: BLE001
        return Check(FAIL, "browser", f"{type(exc).__name__}: {exc}")
    if executable.exists():
        return Check(PASS, "browser", str(executable))
    return Check(
        FAIL,
        "browser",
        "Playwright's Chromium is not installed. Run: python -m playwright install chromium",
    )


def check_env_file(path: Path = Path(".env")) -> Check:
    """Also the cheapest test that this is the project directory at all.

    Every path in the settings is relative. Started from anywhere else, the run
    would quietly create an empty database and have nothing to do.
    """
    if path.exists():
        return Check(PASS, ".env", str(path.resolve()))
    return Check(
        FAIL,
        ".env",
        f"not found in {Path.cwd()}. Either this is not the project directory, "
        f"or .env was not copied across.",
    )


def check_store(db_path: Path) -> list[Check]:
    """The job store, read without creating it if it is absent."""
    if not db_path.exists():
        return [
            Check(
                FAIL,
                "database",
                f"{db_path.resolve()} does not exist. Copy data/txtav.db across, "
                f"or run `txtav import` to start from nothing.",
            )
        ]

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        checks = []
        parts = conn.execute("SELECT COUNT(*) n FROM parts").fetchone()["n"]
        checks.append(
            Check(PASS, "parts", f"{parts:,} loaded")
            if parts
            else Check(FAIL, "parts", "none loaded. Run `txtav import`.")
        )

        halt = conn.execute(
            "SELECT id, kind FROM halts WHERE cleared_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
        if halt is not None:
            checks.append(
                Check(
                    FAIL,
                    "halt",
                    f"#{halt['id']} {halt['kind']} is open. Every run refuses to "
                    f"start until it is read and cleared.",
                )
            )
        else:
            checks.append(Check(PASS, "halt", "none open"))

        checks.append(_check_cycle(conn))
        return checks
    finally:
        conn.close()


def _check_cycle(conn: sqlite3.Connection) -> Check:
    row = conn.execute(
        "SELECT id, started_at, ends_at FROM cycles WHERE status = 'running'"
        " ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return Check(PASS, "cycle", "none running; the first run opens one")

    from txtav import runner

    progress = runner.cycle_progress(conn, int(row["id"]))
    if progress is None or progress.on_schedule:
        return Check(PASS, "cycle", f"{row['id']} ends {row['ends_at']} UTC, on track")
    return Check(
        WARN,
        "cycle",
        f"{row['id']} opened {row['started_at']} UTC and is {progress.behind_by:,} "
        f"parts behind its own deadline. If the real run starts only now, run "
        f"`txtav cycle-restart --yes` first; results already captured are kept.",
    )


def check_alerts(webhook: str) -> Check:
    if webhook:
        return Check(PASS, "alerts", "webhook configured")
    return Check(
        WARN,
        "alerts",
        "no TXTAV_ALERT_WEBHOOK. The run works, but a halt or an expired session "
        "on a remote machine will reach nobody until someone looks.",
    )


def check_disk(path: Path, minimum_gb: float = MIN_FREE_GB, usage=shutil.disk_usage) -> Check:
    free_gb = usage(path.resolve() if path.exists() else Path.cwd()).free / 1024**3
    if free_gb >= minimum_gb:
        return Check(PASS, "disk", f"{free_gb:.1f} GB free")
    return Check(
        WARN,
        "disk",
        f"{free_gb:.1f} GB free, less than {minimum_gb:g} GB. Logs, screenshots and "
        f"the database grow over a cycle.",
    )


def check_sheet(settings) -> Check:
    """Prove the sheet can be written with nobody at the keyboard."""
    if not settings.sheet_id:
        return Check(FAIL, "sheet", "TXTAV_SHEET_ID is not set")

    from txtav import exporter, sheets

    try:
        creds = sheets.authorise(
            settings.google_client_secrets,
            settings.google_token,
            open_browser=False,
            interactive=False,
        )
        service = sheets.build_service(creds)
        problems = sheets.check_header(
            service, settings.sheet_id, settings.sheet_tab, exporter.COLUMNS
        )
    except Exception as exc:  # noqa: BLE001
        return Check(
            FAIL,
            "sheet",
            f"{type(exc).__name__}: {exc}. Copy secrets/ across, or run "
            f"`txtav sheet-auth` on this machine.",
        )
    if problems:
        return Check(
            FAIL,
            "sheet",
            "reachable, but the header does not match: " + "; ".join(problems),
        )
    return Check(PASS, "sheet", f"token works and the {settings.sheet_tab} header matches")


def run_all(settings) -> list[Check]:
    checks = [check_python(), check_packages(), check_env_file()]
    checks.append(check_browser())
    checks += check_store(Path(settings.db_path))
    checks.append(check_alerts(settings.alert_webhook))
    checks.append(check_disk(Path(settings.db_path).parent))
    checks.append(check_sheet(settings))
    return checks
