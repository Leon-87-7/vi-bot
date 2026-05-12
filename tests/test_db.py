"""Tests for db.py — all use a real aiosqlite database via tmp_path."""

import re
import pytest

from db import (
    init_db,
    create_job,
    get_job,
    update_job,
    transition_status,
    check_dedup,
    get_stuck_processing_jobs,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """Return a fresh DB path with the schema already initialised."""
    path = str(tmp_path / "test.db")
    await init_db(path)
    return path


# ---------------------------------------------------------------------------
# 1. Job ID format
# ---------------------------------------------------------------------------


async def test_job_id_format(db):
    job = await create_job(db, 1, 1, "https://example.com/v", "short")
    assert re.fullmatch(r"\d{8}_\d{6}_[A-Z]{4}", job["id"]), (
        f"Unexpected job id format: {job['id']!r}"
    )


# ---------------------------------------------------------------------------
# 2. Created job fields
# ---------------------------------------------------------------------------


async def test_create_job_fields(db):
    job = await create_job(db, 42, 99, "https://youtu.be/abc", "long")
    assert job["chat_id"] == 42
    assert job["message_id"] == 99
    assert job["url"] == "https://youtu.be/abc"
    assert job["pipeline_type"] == "long"
    assert job["status"] == "pending"
    assert job["attempt"] == 0
    assert job["error_msg"] is None
    assert job["drive_url"] is None
    assert job["processing_time_ms"] is None
    assert job["created_at"] is not None
    assert job["updated_at"] is not None


# ---------------------------------------------------------------------------
# 3-6. Dedup routing
# ---------------------------------------------------------------------------


async def test_dedup_first_insert_is_new(db):
    result = await check_dedup(db, "https://example.com/video1")
    assert result["route"] == "new"
    assert result["job"] is None


async def test_dedup_done_with_drive_url_is_recover(db):
    job = await create_job(db, 1, 1, "https://example.com/video2", "short")
    await update_job(db, job["id"], status="done", drive_url="https://drive.google.com/file/x")
    result = await check_dedup(db, "https://example.com/video2")
    assert result["route"] == "recover"
    assert result["job"]["id"] == job["id"]


async def test_dedup_error_status_is_error(db):
    job = await create_job(db, 1, 1, "https://example.com/video3", "short")
    await update_job(db, job["id"], status="error", error_msg="boom")
    result = await check_dedup(db, "https://example.com/video3")
    assert result["route"] == "error"
    assert result["job"]["id"] == job["id"]


@pytest.mark.parametrize("in_progress_status", ["pending", "processing"])
async def test_dedup_in_progress_is_recover(db, in_progress_status):
    job = await create_job(db, 1, 1, "https://example.com/video4", "short")
    if in_progress_status == "processing":
        await update_job(db, job["id"], status="processing")
    result = await check_dedup(db, "https://example.com/video4")
    assert result["route"] == "recover"
    assert result["job"]["id"] == job["id"]


# ---------------------------------------------------------------------------
# 7-11. Status transitions
# ---------------------------------------------------------------------------


async def test_transition_pending_to_processing(db):
    job = await create_job(db, 1, 1, "https://example.com/t1", "short")
    await transition_status(db, job["id"], "pending", "processing")
    updated = await get_job(db, job["id"])
    assert updated["status"] == "processing"


async def test_transition_processing_to_done(db):
    job = await create_job(db, 1, 1, "https://example.com/t2", "short")
    await transition_status(db, job["id"], "pending", "processing")
    await transition_status(db, job["id"], "processing", "done")
    updated = await get_job(db, job["id"])
    assert updated["status"] == "done"


async def test_transition_processing_to_error(db):
    job = await create_job(db, 1, 1, "https://example.com/t3", "short")
    await transition_status(db, job["id"], "pending", "processing")
    await transition_status(db, job["id"], "processing", "error")
    updated = await get_job(db, job["id"])
    assert updated["status"] == "error"


async def test_transition_error_to_pending(db):
    job = await create_job(db, 1, 1, "https://example.com/t4", "short")
    await transition_status(db, job["id"], "pending", "processing")
    await transition_status(db, job["id"], "processing", "error")
    await transition_status(db, job["id"], "error", "pending")
    updated = await get_job(db, job["id"])
    assert updated["status"] == "pending"


async def test_transition_illegal_raises(db):
    job = await create_job(db, 1, 1, "https://example.com/t5", "short")
    await transition_status(db, job["id"], "pending", "processing")
    await transition_status(db, job["id"], "processing", "done")
    with pytest.raises(ValueError):
        await transition_status(db, job["id"], "done", "processing")


# ---------------------------------------------------------------------------
# 12. Stuck processing jobs (crash recovery)
# ---------------------------------------------------------------------------


async def test_get_stuck_processing_jobs(db):
    j1 = await create_job(db, 1, 1, "https://example.com/s1", "short")
    j2 = await create_job(db, 1, 2, "https://example.com/s2", "short")
    j3 = await create_job(db, 1, 3, "https://example.com/s3", "short")

    # Move j1 and j2 to processing
    await transition_status(db, j1["id"], "pending", "processing")
    await transition_status(db, j2["id"], "pending", "processing")
    # j3 stays pending

    stuck = await get_stuck_processing_jobs(db)
    ids = {j["id"] for j in stuck}
    assert j1["id"] in ids
    assert j2["id"] in ids
    assert j3["id"] not in ids


# ---------------------------------------------------------------------------
# 13. /refresh: always inserts new row even when a done job exists
# ---------------------------------------------------------------------------


async def test_force_refresh_inserts_new_row(db):
    url = "https://example.com/force1"
    j1 = await create_job(db, 1, 1, url, "short")
    await update_job(db, j1["id"], status="done", drive_url="https://drive.google.com/file/y")

    # Simulate /refresh — create_job is called unconditionally
    j2 = await create_job(db, 1, 2, url, "short")
    assert j2["id"] != j1["id"]
    assert j2["status"] == "pending"


# ---------------------------------------------------------------------------
# 14. All four pipeline_type values are stored and retrieved correctly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pipeline_type",
    ["short", "long", "short_transcript", "long_transcript"],
)
async def test_pipeline_type_roundtrip(db, pipeline_type):
    job = await create_job(db, 1, 1, "https://example.com/pt", pipeline_type)
    fetched = await get_job(db, job["id"])
    assert fetched["pipeline_type"] == pipeline_type
