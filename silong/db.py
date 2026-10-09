"""SQLite storage for reports. The reports table is also the worker's job queue.

Every operation opens its own connection and runs in an explicit short
transaction, so callers on different threads never share a connection.
"""

import json
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


def _write(db_path: PathLike, sql: str, params: tuple) -> int:
    """Run one guarded UPDATE in its own transaction; return the rows changed."""
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = conn.execute(sql, params).rowcount
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return changed


def claim_next(db_path: PathLike) -> Optional[dict]:
    """Move the oldest queued report to `extracting` and return it, or None if none is queued."""
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT id FROM reports WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE reports SET status = 'extracting', error = NULL WHERE id = ?",
                    (row["id"],),
                )
                row = conn.execute("SELECT * FROM reports WHERE id = ?", (row["id"],)).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return dict(row) if row is not None else None


def save_extraction(
    db_path: PathLike, report_id: int, fields: dict, urgency: str, reason: str, llm_ms: int
) -> bool:
    """Store a worker result. Discarded (returns False) unless the report is still
    `extracting` and not edited by staff."""
    return (
        _write(
            db_path,
            "UPDATE reports SET fields_json = ?, urgency = ?, urgency_reason = ?, llm_ms = ?,"
            " status = 'done', error = NULL"
            " WHERE id = ? AND status = 'extracting' AND edited = 0",
            (json.dumps(fields), urgency, reason, llm_ms, report_id),
        )
        == 1
    )


def mark_failed(db_path: PathLike, report_id: int, message: str) -> bool:
    """Mark a report failed with `message`, unless it is no longer `extracting`."""
    return (
        _write(
            db_path,
            "UPDATE reports SET status = 'failed', error = ? WHERE id = ? AND status = 'extracting'",
            (message, report_id),
        )
        == 1
    )


def recover_interrupted(db_path: PathLike) -> int:
    """Requeue work interrupted by a stop; return how many reports were reset.

    Only `status` changes: originals, fields, edits, resolved state and timings stay.
    """
    return _write(
        db_path,
        "UPDATE reports SET status = 'queued' WHERE status IN ('transcribing', 'extracting')",
        (),
    )


def list_reports(db_path: PathLike) -> list[dict]:
    """All reports, oldest receipt first, with id breaking ties."""
    with closing(connect(db_path)) as conn:
        rows = conn.execute("SELECT * FROM reports ORDER BY created_at, id").fetchall()
    return [dict(row) for row in rows]
