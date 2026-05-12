import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from drive import slugify
from pipeline import run_short_job


# ---------------------------------------------------------------------------
# 1. slugify
# ---------------------------------------------------------------------------


def test_slugify_spaces_to_underscore():
    assert slugify("hello world") == "hello_world"


def test_slugify_special_chars_stripped():
    assert slugify("Hello, World! #1") == "hello_world_1"


def test_slugify_truncation():
    long_text = "a" * 80
    result = slugify(long_text)
    assert len(result) == 60
    assert result == "a" * 60


def test_slugify_collapses_runs():
    assert slugify("foo   bar---baz") == "foo_bar_baz"


def test_slugify_strips_leading_trailing_underscore():
    assert slugify("__hello__") == "hello"


# ---------------------------------------------------------------------------
# 2. run_short_job — happy path
# ---------------------------------------------------------------------------


@pytest.fixture
def settings():
    s = MagicMock()
    s.db_path = ":memory:"
    s.transcript_url = "http://localhost:5050"
    s.brave_api_key = ""
    s.google_drive_folder_short = "folder123"
    s.google_sheets_id_short = "sheet123"
    return s


@pytest.fixture
def job():
    return {
        "id": "20260512_120000_ABCD",
        "url": "https://example.com/short",
        "chat_id": 42,
        "pipeline_type": "short",
        "attempt": 0,
    }


@pytest.mark.asyncio
async def test_run_short_job_happy_path(settings, job):
    frames = [{"index": 0, "timestamp_s": 0.0, "base64": "aGVsbG8=", "mime_type": "image/jpeg"}]
    report_text = "## Summary\nGreat video.\n\n## Key Points\n- Point 1\n\n## Timestamps\n00:00:00 — start\n\n## Tags\nfoo, bar"

    mock_http_response = MagicMock()
    mock_http_response.raise_for_status = MagicMock()
    mock_http_response.json = MagicMock(return_value=frames)

    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)
    mock_http_client.get = AsyncMock(return_value=mock_http_response)

    with (
        patch("pipeline.transition_status", new_callable=AsyncMock) as mock_transition,
        patch("pipeline.httpx.AsyncClient", return_value=mock_http_client),
        patch("pipeline.analyse_short", new_callable=AsyncMock, return_value=report_text),
        patch("pipeline.asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread,
        patch("pipeline.update_job", new_callable=AsyncMock) as mock_update,
        patch("pipeline.send_message", new_callable=AsyncMock),
    ):
        mock_to_thread.side_effect = [
            "https://drive.google.com/file/abc",
            None,
        ]

        gemini_client = MagicMock()
        drive_svc = MagicMock()
        sheets_svc = MagicMock()

        await run_short_job(job, settings, gemini_client, drive_svc, sheets_svc)

        mock_transition.assert_called_once_with(
            settings.db_path, job["id"], "pending", "processing"
        )

        update_calls = mock_update.call_args_list
        assert any(
            call.kwargs.get("status") == "done" and call.kwargs.get("drive_url") == "https://drive.google.com/file/abc"
            for call in update_calls
        ), f"Expected done+drive_url update, got: {update_calls}"


# ---------------------------------------------------------------------------
# 3. run_short_job — httpx failure propagates, worker sets error status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_short_job_httpx_failure(settings, job):
    mock_http_response = MagicMock()
    mock_http_response.raise_for_status = MagicMock(side_effect=Exception("connection refused"))

    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)
    mock_http_client.get = AsyncMock(return_value=mock_http_response)

    with (
        patch("pipeline.transition_status", new_callable=AsyncMock),
        patch("pipeline.httpx.AsyncClient", return_value=mock_http_client),
        patch("pipeline.update_job", new_callable=AsyncMock) as mock_update,
        patch("pipeline.send_sticker", new_callable=AsyncMock),
    ):
        queue = asyncio.Queue()
        await queue.put(job)

        from pipeline import worker

        gemini_client = MagicMock()
        drive_svc = MagicMock()
        sheets_svc = MagicMock()

        task = asyncio.create_task(
            worker(queue, settings, gemini_client, drive_svc, sheets_svc)
        )
        await queue.join()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        error_calls = [
            c for c in mock_update.call_args_list if c.kwargs.get("status") == "error"
        ]
        assert len(error_calls) >= 1


# ---------------------------------------------------------------------------
# 4. Queue full → webhook returns busy message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_queue_full():
    import main as main_module

    @asynccontextmanager
    async def mock_lifespan(app):
        app.state.settings = MagicMock(
            db_path=":memory:",
            telegram_bot_token="tok",
            webhook_url="http://localhost",
            telegram_webhook_secret="",
            telegram_sticker_gemini_fail="sticker",
            telegram_sticker_drive_fail="sticker2",
            transcript_url="http://localhost:5050",
            brave_api_key="",
            google_service_account_json="/tmp/sa.json",
            google_drive_folder_short="f1",
            google_drive_folder_long="f2",
            google_sheets_id_short="s1",
            google_sheets_id_long="s2",
            num_workers=1,
        )
        full_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait({"dummy": True})
        app.state.queue = full_queue
        yield

    test_app = FastAPI(lifespan=mock_lifespan)

    @test_app.post("/webhook")
    async def _webhook(request: Request):
        return await main_module.webhook(request)

    with (
        patch("main.classify_url", return_value={"type": "short", "url": "https://example.com/v", "force": False}),
        patch("main.check_dedup", new_callable=AsyncMock, return_value={"route": "new", "job": None}),
        patch("main.create_job", new_callable=AsyncMock, return_value={
            "id": "JOB1", "url": "https://example.com/v",
            "pipeline_type": "short", "chat_id": 1, "attempt": 0,
        }),
        patch("main.send_message", new_callable=AsyncMock) as mock_send,
    ):
        with TestClient(test_app) as client:
            payload = {"message": {"chat": {"id": 1}, "message_id": 1, "text": "https://example.com/v"}}
            resp = client.post("/webhook", json=payload)
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
            busy_calls = [
                c for c in mock_send.call_args_list
                if "busy" in str(c).lower()
            ]
            assert len(busy_calls) >= 1


# ---------------------------------------------------------------------------
# 5. Worker — unknown pipeline_type is a no-op, task_done still called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_unknown_pipeline_type_is_noop(settings, job):
    unknown_job = {**job, "pipeline_type": "future_type"}

    with patch("pipeline.update_job", new_callable=AsyncMock) as mock_update:
        queue = asyncio.Queue()
        await queue.put(unknown_job)

        from pipeline import worker

        task = asyncio.create_task(
            worker(queue, settings, MagicMock(), MagicMock(), MagicMock())
        )
        await queue.join()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_update.assert_not_called()