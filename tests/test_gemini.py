"""Tests for gemini.py — Text client (analyze_transcript)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gemini import analyze_transcript, GeminiTextError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(payload: dict) -> MagicMock:
    """Return a mock whose .text is the JSON-serialised *payload*."""
    mock = MagicMock()
    mock.text = json.dumps(payload)
    return mock


_VALID_PAYLOAD = {
    "category": "Learning & How-To",
    "topic": "Python async programming",
    "objective": "Learn how to write async code in Python.",
    "action_points": ["Install asyncio", "Write coroutines"],
    "tools": ["Python", "asyncio"],
    "market_data": "",
}


def _patch_client(return_value):
    """Patch genai.Client so aio.models.generate_content returns *return_value*."""
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=return_value)
    return patch("gemini._make_client", return_value=mock_client)


# ---------------------------------------------------------------------------
# 1. Transcript is truncated to 80 000 chars before sending
# ---------------------------------------------------------------------------


async def test_transcript_truncated_to_80k():
    long_transcript = "x" * 200_000
    captured: list[str] = []

    async def fake_generate(model, contents, config):
        captured.append(contents)
        return _mock_response(_VALID_PAYLOAD)

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = fake_generate

    with patch("gemini._make_client", return_value=mock_client):
        await analyze_transcript(long_transcript)

    # The transcript is all 'x'; after 80k truncation the 80_001st 'x' must be absent
    sent: str = captured[0]
    assert "x" * 80_001 not in sent, "Transcript was not truncated to 80 000 chars"
    assert "x" * 80_000 in sent, "First 80 000 chars of transcript must be present"


async def test_short_transcript_not_truncated():
    """Transcripts under 80 000 chars are sent in full."""
    short_transcript = "hello world " * 100
    captured: list[str] = []

    async def fake_generate(model, contents, config):
        captured.append(contents)
        return _mock_response(_VALID_PAYLOAD)

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = fake_generate

    with patch("gemini._make_client", return_value=mock_client):
        await analyze_transcript(short_transcript)

    assert short_transcript in captured[0]


# ---------------------------------------------------------------------------
# 2. Returned dict has all required keys with correct types
# ---------------------------------------------------------------------------


async def test_returns_all_required_keys():
    with _patch_client(_mock_response(_VALID_PAYLOAD)):
        result = await analyze_transcript("some transcript")

    assert set(result.keys()) >= {
        "category", "topic", "objective", "action_points", "tools", "market_data"
    }
    assert isinstance(result["action_points"], list)
    assert isinstance(result["tools"], list)
    assert isinstance(result["market_data"], str)


async def test_returns_correct_values():
    with _patch_client(_mock_response(_VALID_PAYLOAD)):
        result = await analyze_transcript("transcript text")

    assert result["category"] == "Learning & How-To"
    assert result["topic"] == "Python async programming"
    assert result["action_points"] == ["Install asyncio", "Write coroutines"]


# ---------------------------------------------------------------------------
# 3. GeminiTextError raised on API failure
# ---------------------------------------------------------------------------


async def test_raises_on_api_exception():
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(side_effect=RuntimeError("quota exceeded"))

    with patch("gemini._make_client", return_value=mock_client):
        with pytest.raises(GeminiTextError, match="quota exceeded"):
            await analyze_transcript("transcript")


async def test_raises_on_invalid_json():
    mock_response = MagicMock()
    mock_response.text = "not valid json {{{"
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    with patch("gemini._make_client", return_value=mock_client):
        with pytest.raises(GeminiTextError):
            await analyze_transcript("transcript")


async def test_raises_on_missing_keys():
    incomplete = {"category": "Learning & How-To", "topic": "AI"}  # missing 4 keys

    with _patch_client(_mock_response(incomplete)):
        with pytest.raises(GeminiTextError, match="missing keys"):
            await analyze_transcript("transcript")


# ---------------------------------------------------------------------------
# 4. Empty transcript is accepted (no crash)
# ---------------------------------------------------------------------------


async def test_empty_transcript_accepted():
    with _patch_client(_mock_response(_VALID_PAYLOAD)):
        result = await analyze_transcript("")
    assert result["category"] == "Learning & How-To"
