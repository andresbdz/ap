"""SQLite job store. This, not the spreadsheet, is the source of truth.

The run is 109,589 parts at one every 11 seconds across 14 days of continuous
operation, so it has to survive a crash, a reboot and a network drop without
losing its place. Every part is committed as it completes, which caps the loss
from an abrupt kill at a single part. The Google Sheet is a projection of this
database, written in batches, never the place state is kept.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

STATUS_PENDING = "pending"
STATUS_IN_FLIGHT = "in_flight"
STATUS_SUCCESS = "success"
STATUS_NOT_FOUND = "not_found"
STATUS_FAILED = "failed"
STATUS_MANUAL_REVIEW = "manual_review"

TERMINAL_STATUSES = (
    STATUS_SUCCESS,
    STATUS_NOT_FOUND,
    STATUS_FAILED,
    STATUS_MANUAL_REVIEW,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per part number in the client's sheet, after TXT filtering.
CREATE TABLE IF NOT EXISTS parts (
    pn           TEXT PRIMARY KEY,
    dedupe_key   TEXT NOT NULL,
    wh           TEXT NOT NULL,
    source_row   INTEGER NOT NULL,
    path_hostile INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS parts_dedupe ON parts(dedupe_key);

-- A cycle is one full pass over the part list.
CREATE TABLE IF NOT EXISTS cycles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    ends_at     TEXT,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS jobs (
    cycle_id    INTEGER NOT NULL REFERENCES cycles(id),
    pn          TEXT    NOT NULL REFERENCES parts(pn),
    status      TEXT    NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT,
    PRIMARY KEY (cycle_id, pn)
);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(cycle_id, status);

-- list_price, discount_pct and tab_active are not required by the output
-- sheet. They are recorded so that if the client later changes their mind
-- about which price or which tab to use, the answer can be produced from
-- this table rather than by crawling 109,589 pages a second time.
CREATE TABLE IF NOT EXISTS results (
    cycle_id       INTEGER NOT NULL REFERENCES cycles(id),
    pn             TEXT    NOT NULL REFERENCES parts(pn),
    description    TEXT,
    availability   TEXT NOT NULL DEFAULT 'available',
    quantity       INTEGER,
    sale_price     TEXT,
    list_price     TEXT,
    discount_pct   TEXT,
    condition_code TEXT,
    core_value     TEXT,
    outright_price TEXT,
    part_type      TEXT,
    alt_pn         TEXT,
    tab_active     TEXT,
    price_ref      TEXT NOT NULL DEFAULT 'TXT',
    conflict       INTEGER NOT NULL DEFAULT 0,
    conflict_note  TEXT,
    html_sha256    TEXT,
    scraped_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (cycle_id, pn)
);

-- Halts, auth expiry, selector misses. Append only; this is what the daily
-- summary and the post mortem of any stoppage are built from.
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL DEFAULT (datetime('now')),
    level  TEXT NOT NULL,
    kind   TEXT NOT NULL,
    pn     TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);

-- An open row here blocks every run until an operator clears it by hand.
-- Nothing in the code may clear a halt automatically.
CREATE TABLE IF NOT EXISTS halts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    raised_at   TEXT NOT NULL DEFAULT (datetime('now')),
    kind        TEXT NOT NULL,
    detail      TEXT,
    cleared_at  TEXT,
    cleared_by  TEXT
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open the store, creating it and its parent directory if needed."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a store was first created.

    CREATE TABLE IF NOT EXISTS leaves an existing table untouched, so a new
    column has to be added explicitly or a store built by an earlier version
    fails at the first insert.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(results)")}
    for column, definition in (
        ("availability", "TEXT NOT NULL DEFAULT 'available'"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE results ADD COLUMN {column} {definition}")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction. isolation_level is None so this is the only one."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def open_halt(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The oldest uncleared halt, or None when the system is free to run."""
    return conn.execute(
        "SELECT * FROM halts WHERE cleared_at IS NULL ORDER BY id LIMIT 1"
    ).fetchone()


def raise_halt(conn: sqlite3.Connection, kind: str, detail: str) -> int:
    """Record a stop condition. Never cleared from code, only by an operator."""
    cur = conn.execute(
        "INSERT INTO halts(kind, detail) VALUES (?, ?)", (kind, detail)
    )
    conn.execute(
        "INSERT INTO events(level, kind, detail) VALUES ('critical', ?, ?)",
        (f"halt:{kind}", detail),
    )
    return int(cur.lastrowid)


def clear_halt(conn: sqlite3.Connection, halt_id: int, cleared_by: str) -> None:
    conn.execute(
        "UPDATE halts SET cleared_at = datetime('now'), cleared_by = ? "
        "WHERE id = ? AND cleared_at IS NULL",
        (cleared_by, halt_id),
    )


def log_event(
    conn: sqlite3.Connection,
    level: str,
    kind: str,
    detail: str,
    pn: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events(level, kind, pn, detail) VALUES (?, ?, ?, ?)",
        (level, kind, pn, detail),
    )
