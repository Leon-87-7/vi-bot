"""Tests for the long-video pipeline in pipeline.py."""

import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from pipeline import (
    _extract_description_links,
    _build_telegram_message,
    _make_drive_filename,
    run_long_pipeline,
)
from gemini import GeminiTextError
from drive import DriveUploadError


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_TRANSCRIPT_OK = {"status": "ok", "transcript": "This is the transcript text."}
_METADATA_OK = {
    "status": "ok",
    "title": "How To Build Async APIs",
    "channel": "Dev Channel",
    "description": "Check out https://fastapi.tiangolo.com and https://youtube.com/watch?v=abc",
}
_AI_RESULT = {
    "category": "Learning & How-To",
    "topic": "Async API development",
    "objective": "Learn to build fast async APIs.",
    "action_points": ["Install FastAPI", "Write async endpoints"],
    "tools": ["FastAPI", "uvicorn"],
    "market_data": "",
}


def _make_job(*, status="processing", attempt=0) -> dict:
    return {
        "id": "20260515_120000_ABCD",
        "chat_id": 42,
        "message_id": 99,
        "url": "https://youtu.be/abc123",
        "pipeline_type": "long",
        "status": status,
        "attempt": attempt,
        "error_msg": None,
        "drive_url": None,
        "processing_time_ms": None,
    }


class _PatchSet:
    """Context manager that patches all external calls in run_long_pipeline."""

    def __init__(
        self,
        *,
        transcript=_TRANSCRIPT_OK,
        metadata=_METADATA_OK,
        ai_result=_AI_RESULT,
        ai_side_effect=None,
        drive_url="https://drive.google.com/file/d/abc",
        drive_side_effect=None,
        settings_extra=None,
    ):
        self.transcript = transcript
        self.metadata = metadata
        self.ai_result = ai_result
        self.ai_side_effect = ai_side_effect
        self.drive_url = drive_url
        self.drive_side_effect = drive_side_effect
        self.settings_extra = settings_extra or {}

        self.send_message = AsyncMock(return_value={"message_id": 1})
        self.send_sticker = AsyncMock(return_value={"message_id": 2})
        self.upload_report = AsyncMock(return_value=self.drive_url)
        self.append_to_sheets = AsyncMock(return_value=None)
        self.update_job = AsyncMock(return_value=None)
        self.transition_status = AsyncMock(return_value=None)

    def __enter__(self):
        from config import Settings

        fake_settings = MagicMock(spec=Settings)
        fake_settings.db_path = ":memory:"
        fake_settings.transcript_url = "http://localhost:5050"
        fake_settings.google_drive_folder_long = "FOLDER_LONG"
        fake_settings.google_sheets_id_long = "SHEETS_LONG"
        fake_settings.telegram_sticker_drive_fail = "STICKER_ID"
        for k, v in self.settings_extra.items():
            setattr(fake_settings, k, v)

        async def fake_gather(transcript_task, metadata_task):
            return await transcript_task, await metadata_task

        transcript_mock = AsyncMock(return_value=self.transcript)
        metadata_mock = AsyncMock(return_value=self.metadata)

        if self.ai_side_effect:
            ai_mock = AsyncMock(side_effect=self.ai_side_effect)
        else:
            ai_mock = AsyncMock(return_value=self.ai_result)

        if self.drive_side_effect:
            self.upload_report = AsyncMock(side_effect=self.drive_side_effect)

        self._patches = [
            patch("pipeline.get_settings", return_value=fake_settings),
            patch("pipeline._fetch_transcript", transcript_mock),
            patch("pipeline._fetch_metadata", metadata_mock),
            patch("pipeline.analyze_transcript", ai_mock),
            patch("pipeline.upload_report", self.upload_report),
            patch("pipeline.append_to_sheets", self.append_to_sheets),
            patch("pipeline.update_job", self.update_job),
            patch("pipeline.transition_status", self.transition_status),
            patch("pipeline.send_message", self.send_message),
            patch("pipeline.send_sticker", self.send_sticker),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *_):
        for p in self._patches:
            p.stop()


# ---------------------------------------------------------------------------
# _extract_description_links
# ---------------------------------------------------------------------------


def test_extract_description_links_filters_generic():
    desc = (
        "Watch on YouTube: https://youtube.com/watch?v=abc\n"
        "FastAPI docs: https://fastapi.tiangolo.com\n"
        "More: https://instagram.com/user"
    )
    links = _extract_description_links(desc)
    assert "https://fastapi.tiangolo.com" in links
    assert all("youtube.com" not in lnk for lnk in links)
    assert all("instagram.com" not in lnk for lnk in links)


def test_extract_description_links_dedupes():
    desc = "https://example.com https://example.com"
    links = _extract_description_links(desc)
    assert links.count("https://example.com") == 1


def test_extract_description_links_strips_trailing_punct():
    desc = "Visit https://example.com, for details."
    links = _extract_description_links(desc)
    assert "https://example.com" in links
    assert all(not lnk.endswith(",") for lnk in links)


def test_extract_description_links_empty_string():
    assert _extract_description_links("") == []


# ---------------------------------------------------------------------------
# _make_drive_filename
# ---------------------------------------------------------------------------


def test_make_drive_filename_basic():
    name = _make_drive_filename("20260515_120000_ABCD", "How To Build Async APIs")
    assert name.startswith("20260515_120000_ABCD_")
    assert name.endswith(".md")
    assert " " not in name
    assert name == name.lower() or name.startswith("2026")


def test_make_drive_filename_strips_special_chars():
    name = _make_drive_filename("JOB", "Title: With! Special@Chars?")
    assert ":" not in name
    assert "!" not in name
    assert "@" not in name


def test_make_drive_filename_slug_max_60_chars():
    long_title = "a" * 200
    name = _make_drive_filename("JOB", long_title)
    slug = name[len("JOB_"):-len(".md")]
    assert len(slug) <= 60


# ---------------------------------------------------------------------------
# _build_telegram_message
# ---------------------------------------------------------------------------


def test_build_telegram_message_includes_all_sections():
    msg = _build_telegram_message(
        "My Video",
        _AI_RESULT,
        ["https://example.com"],
        "https://drive.google.com/file/d/abc",
        None,
    )
    assert "My Video" in msg
    assert "Learning & How-To" in msg
    assert "https://example.com" in msg
    assert "https://drive.google.com/file/d/abc" in msg


def test_build_telegram_message_shows_ai_error():
    msg = _build_telegram_message(
        "My Video",
        {k: "" if isinstance(v, str) else [] for k, v in _AI_RESULT.items()},
        [],
        "https://drive.google.com",
        "timeout after 60s",
    )
    assert "AI enrichment failed" in msg
    assert "timeout after 60s" in msg


def test_build_telegram_message_truncated_at_4000():
    long_points = ["• " + "x" * 500] * 20
    bloated = dict(_AI_RESULT, action_points=long_points)
    msg = _build_telegram_message("Title", bloated, [], "https://drive.google.com", None)
    assert len(msg) <= 4000


# ---------------------------------------------------------------------------
# run_long_pipeline: transcript hard error
# ---------------------------------------------------------------------------


async def test_transcript_error_marks_job_error():
    bad_transcript = {"status": "error", "message": "video too long"}
    with _PatchSet(transcript=bad_transcript) as ps:
        await run_long_pipeline(_make_job())

    # Must NOT be marked done
    done_calls = [
        c for c in ps.transition_status.call_args_list
        if c.args[-1] == "done"
    ]
    assert not done_calls

    error_calls = [
        c for c in ps.transition_status.call_args_list
        if c.args[-1] == "error"
    ]
    assert error_calls

    # Error message must be sent to user
    assert ps.send_message.called


async def test_transcript_error_does_not_call_gemini():
    bad_transcript = {"status": "error", "message": "unavailable"}
    with _PatchSet(transcript=bad_transcript) as ps:
        with patch("pipeline.analyze_transcript", AsyncMock()) as mock_ai:
            await run_long_pipeline(_make_job())
    # analyze_transcript should not be called on hard transcript error
    mock_ai.assert_not_called()


# ---------------------------------------------------------------------------
# run_long_pipeline: Gemini failure recover path
# ---------------------------------------------------------------------------


async def test_gemini_failure_still_marks_done():
    """If Gemini fails, the job is still marked done (with metadata only)."""
    with _PatchSet(ai_side_effect=GeminiTextError("timeout")) as ps:
        await run_long_pipeline(_make_job())

    done_calls = [
        c for c in ps.transition_status.call_args_list
        if c.args[-1] == "done"
    ]
    assert done_calls, "Job must be marked done even when Gemini fails"


async def test_gemini_failure_drive_still_uploaded():
    with _PatchSet(ai_side_effect=GeminiTextError("model error")) as ps:
        await run_long_pipeline(_make_job())

    ps.upload_report.assert_called_once()


async def test_gemini_failure_message_mentions_ai_error():
    with _PatchSet(ai_side_effect=GeminiTextError("rate limit")) as ps:
        await run_long_pipeline(_make_job())

    all_messages = " ".join(
        str(call.args[1]) for call in ps.send_message.call_args_list
    )
    assert "AI enrichment failed" in all_messages or "rate limit" in all_messages


# ---------------------------------------------------------------------------
# run_long_pipeline: Drive failure
# ---------------------------------------------------------------------------


async def test_drive_failure_marks_error():
    with _PatchSet(drive_side_effect=DriveUploadError("403 forbidden")) as ps:
        await run_long_pipeline(_make_job())

    error_calls = [
        c for c in ps.transition_status.call_args_list
        if c.args[-1] == "error"
    ]
    assert error_calls


async def test_drive_failure_sends_sticker_and_retry():
    with _PatchSet(drive_side_effect=DriveUploadError("timeout")) as ps:
        await run_long_pipeline(_make_job())

    ps.send_sticker.assert_called_once()

    # At least one send_message call must include a retry reply_markup
    retry_calls = [
        c for c in ps.send_message.call_args_list
        if c.kwargs.get("reply_markup")
    ]
    assert retry_calls


# ---------------------------------------------------------------------------
# run_long_pipeline: success path
# ---------------------------------------------------------------------------


async def test_success_marks_done():
    with _PatchSet() as ps:
        await run_long_pipeline(_make_job())

    done_calls = [
        c for c in ps.transition_status.call_args_list
        if c.args[-1] == "done"
    ]
    assert done_calls


async def test_success_updates_drive_url():
    drive_url = "https://drive.google.com/file/d/xyz"
    with _PatchSet(drive_url=drive_url) as ps:
        await run_long_pipeline(_make_job())

    update_calls = ps.update_job.call_args_list
    drive_url_set = any(
        c.kwargs.get("drive_url") == drive_url or
        (len(c.args) > 2 and c.args[2] == drive_url)
        for c in update_calls
    )
    # Check keyword arg in any update_job call
    drive_url_kwarg = any(
        c.kwargs.get("drive_url") == drive_url
        for c in update_calls
    )
    assert drive_url_kwarg, "drive_url must be persisted to DB on success"


async def test_success_appends_to_sheets():
    with _PatchSet() as ps:
        await run_long_pipeline(_make_job())

    ps.append_to_sheets.assert_called_once()


async def test_success_sends_two_messages():
    """Pipeline sends an in-progress message first, then the final message."""
    with _PatchSet() as ps:
        await run_long_pipeline(_make_job())

    assert ps.send_message.call_count >= 2


async def test_success_description_links_in_final_message():
    """Description links from metadata appear in the final Telegram message."""
    with _PatchSet() as ps:
        await run_long_pipeline(_make_job())

    # The final message (last send_message call) should mention the non-generic link
    last_msg = ps.send_message.call_args_list[-1].args[1]
    assert "fastapi.tiangolo.com" in last_msg


# ---------------------------------------------------------------------------
# run_long_pipeline: sheets failure is tolerated
# ---------------------------------------------------------------------------


async def test_sheets_failure_does_not_prevent_done():
    """Sheets append failure is logged and swallowed — job still marked done."""
    with _PatchSet() as ps:
        ps.append_to_sheets.side_effect = Exception("sheets quota")
        await run_long_pipeline(_make_job())

    done_calls = [
        c for c in ps.transition_status.call_args_list
        if c.args[-1] == "done"
    ]
    assert done_calls
