"""Async SQLite database layer for the Video Intelligence Bot job queue."""

import random
import string
from datetime import datetime, timezone

import aiosqlite


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LEGAL_TRANSITIONS: set[tuple[str, str]] = {
    ("pending", "processing"),
    ("processing", "done"),
    ("processing", "error"),
    ("error", "pending"),
}


def _make_job_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase, k=4))
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + suffix


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: aiosqlite.Row) -> dict:
    return dict(row)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    chat_id             INTEGER NOT NULL,
    message_id          INTEGER NOT NULL,
    url                 TEXT NOT NULL,
    pipeline_type       TEXT NOT NULL,
    status              TEXT NOT NULL,
    attempt             INTEGER NOT NULL DEFAULT 0,
    error_msg           TEXT,
    drive_url           TEXT,
    processing_time_ms  INTEGER,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
)
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def init_db(db_path: str) -> None:
    """Create the jobs table if it does not already exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(_CREATE_TABLE_SQL)
        await db.commit()


async def create_job(
    db_path: str,
    chat_id: int,
    message_id: int,
    url: str,
    pipeline_type: str,
) -> dict:
    """Insert a new job with status=pending and return it as a dict."""
    job_id = _make_job_id()
    now = _now_iso()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO jobs
                (id, chat_id, message_id, url, pipeline_type, status,
                 attempt, error_msg, drive_url, processing_time_ms,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, ?, ?)
            """,
            (job_id, chat_id, message_id, url, pipeline_type, now, now),
        )
        await db.commit()

    return await get_job(db_path, job_id)


async def get_job(db_path: str, job_id: str) -> dict | None:
    """Return a job dict or None if not found."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_dict(row) if row is not None else None


async def update_job(db_path: str, job_id: str, **fields) -> None:
    """Update any subset of fields; always refreshes updated_at."""
    if not fields:
        return
    fields["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"UPDATE jobs SET {set_clause} WHERE id = ?", values
        )
        await db.commit()


async def transition_status(
    db_path: str, job_id: str, from_status: str, to_status: str
) -> None:
    """Atomically move a job from from_status to to_status.

    Raises ValueError if the transition is not in the legal set.
    """
    if (from_status, to_status) not in _LEGAL_TRANSITIONS:
        raise ValueError(
            f"Illegal status transition: {from_status!r} -> {to_status!r}"
        )
    now = _now_iso()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE jobs
               SET status = ?, updated_at = ?
             WHERE id = ? AND status = ?
            """,
            (to_status, now, job_id, from_status),
        )
        await db.commit()


async def check_dedup(db_path: str, url: str) -> dict:
    """Return dedup routing information for *url*.

    Uses BEGIN EXCLUSIVE to prevent a race between two simultaneous requests
    for the same URL.

    Returns a dict with keys:
        route  : "recover" | "error" | "new"
        job    : existing job dict or None
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN EXCLUSIVE")
        async with db.execute(
            """
            SELECT * FROM jobs
             WHERE url = ?
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (url,),
        ) as cursor:
            row = await cursor.fetchone()
        # Commit to release the exclusive lock (no writes needed here).
        await db.commit()

    if row is None:
        return {"route": "new", "job": None}

    job = _row_to_dict(row)
    status = job["status"]

    if status == "done" and job.get("drive_url") is not None:
        return {"route": "recover", "job": job}

    if status == "error":
        return {"route": "error", "job": job}

    # pending or processing — already in progress
    return {"route": "recover", "job": job}


async def get_stuck_processing_jobs(db_path: str) -> list[dict]:
    """Return all jobs currently in status=processing (used for crash recovery)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM jobs WHERE status = 'processing'"
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]
