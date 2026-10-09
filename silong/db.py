"""SQLite storage for reports. The reports table is also the worker's job queue.

Every operation opens its own connection and runs in an explicit short
transaction, so callers on different threads never share a connection.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

# Seconds a connection waits for another writer before raising "database is locked".
BUSY_TIMEOUT = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      TEXT    NOT NULL UNIQUE,
    created_at     TEXT    NOT NULL,
    reporter       TEXT,
    raw_text       TEXT,
    audio_path     TEXT,
    transcript     TEXT,
    status         TEXT    NOT NULL DEFAULT 'queued',
    fields_json    TEXT,
    urgency        TEXT,
    urgency_reason TEXT,
    edited         INTEGER NOT NULL DEFAULT 0,
    resolved       INTEGER NOT NULL DEFAULT 0,
    asr_ms         INTEGER,
    llm_ms         INTEGER,
    error          TEXT
)
"""

PathLike = Union[str, Path]


def connect(db_path: PathLike) -> sqlite3.Connection:
    """Open a connection in autocommit mode; callers manage transactions explicitly."""
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db(db_path: PathLike) -> None:
    """Create the reports table if missing. Existing rows are kept."""
    with closing(connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(SCHEMA)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def insert_report(
    db_path: PathLike, client_id: str, raw_text: str, reporter: Optional[str] = None
) -> tuple[dict, bool]:
    """Store a new queued report, or return the existing one for `client_id`.

    Returns (row, created). The row is read back inside the same committed
    transaction, so a returned row is always durable. A repeated `client_id`
    never changes the stored original. Any database error propagates.
    """
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "INSERT INTO reports (client_id, created_at, reporter, raw_text)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(client_id) DO NOTHING",
                (client_id, _utc_now(), reporter, raw_text),
            )
            created = cursor.rowcount == 1
            row = conn.execute(
                "SELECT * FROM reports WHERE client_id = ?", (client_id,)
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return dict(row), created


def list_reports(db_path: PathLike) -> list[dict]:
    """All reports, oldest receipt first, with id breaking ties."""
    with closing(connect(db_path)) as conn:
        rows = conn.execute("SELECT * FROM reports ORDER BY created_at, id").fetchall()
    return [dict(row) for row in rows]
