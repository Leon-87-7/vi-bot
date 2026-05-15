"""Gemini API clients for the Video Intelligence Bot."""

import json
import logging

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_MODEL = "gemini-2.5-flash"

# Generic / social root domains excluded from extracted link lists.
# This list is embedded verbatim in the short-video Vision prompt so Gemini
# applies contextual filtering. For description links (long pipeline) the same
# set is used for Python-level filtering.
GENERIC_ROOTS: list[str] = [
    "youtube.com", "youtu.be", "instagram.com", "twitter.com", "x.com",
    "facebook.com", "fb.com", "tiktok.com", "linkedin.com", "pinterest.com",
    "snapchat.com", "reddit.com", "discord.com", "t.co", "bit.ly",
    "tinyurl.com", "linktr.ee", "linktree.com", "google.com", "gmail.com",
    "goo.gl", "apple.com", "microsoft.com", "spotify.com", "amazon.com",
    "amzn.to", "patreon.com", "buymeacoffee.com", "ko-fi.com",
]

_GENERIC_ROOTS_BLOCK = ", ".join(GENERIC_ROOTS)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_LONG_VIDEO_PROMPT = f"""\
You are analyzing a YouTube video transcript. Classify it into exactly ONE of:
  1. "Learning & How-To" — tutorials, explainers, demos, skill-building
  2. "Business & Marketing" — strategy, entrepreneurship, sales, growth
  3. "News & Analysis" — current events, market trends, commentary

Then extract structured information.

Respond ONLY with valid JSON matching this schema (no markdown fences):
{{
  "category": "<one of the three categories above>",
  "topic": "<2–6 word label>",
  "objective": "<one sentence: what the viewer will learn or gain>",
  "action_points": ["<actionable step>", ...],
  "tools": ["<tool, app, or resource mentioned>", ...],
  "market_data": "<specific statistics or market facts; empty string if none>"
}}

Rules:
- action_points: 3–7 concise items. Empty list if none present.
- tools: every named tool, software, app, or resource. Empty list if none.
- market_data: single string summarising stats/numbers; empty string if none.
- All six keys are required; never omit any.

Transcript:
"""

_SHORT_VIDEO_PROMPT = f"""\
You are analyzing video frames. Extract all specific links and URLs visible \
on screen (text overlays, captions, slides, etc.).

Exclude links whose domain root appears in this list:
{_GENERIC_ROOTS_BLOCK}

Respond ONLY with valid JSON matching this schema (no markdown fences):
{{
  "selected_frame_index": <integer, 0-based index of the most representative frame>,
  "summary": "<2–4 sentence description of the video content>",
  "links": ["<url1>", "<url2>", ...]
}}

Rules:
- Only include links visually present on screen.
- Exclude generic/social domains listed above.
- selected_frame_index must be a valid index into the provided frames.
- links may be an empty list.
"""

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GeminiTextError(Exception):
    """Raised when the Gemini Text API call fails or returns unexpected data."""


class GeminiVisionError(Exception):
    """Raised when the Gemini Vision API call fails or returns unexpected data."""


# ---------------------------------------------------------------------------
# Text client (long-video pipeline)
# ---------------------------------------------------------------------------


def _make_client() -> genai.Client:
    return genai.Client()


async def analyze_transcript(transcript: str) -> dict:
    """Call Gemini Text with *transcript* truncated to 80 000 chars.

    Returns a dict with keys:
        category, topic, objective, action_points, tools, market_data

    Raises GeminiTextError on API failure or unexpected response shape.
    """
    truncated = transcript[:80_000]
    prompt = _LONG_VIDEO_PROMPT + truncated

    try:
        client = _make_client()
        response = await client.aio.models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
            ),
        )
        result = json.loads(response.text)
    except GeminiTextError:
        raise
    except Exception as exc:
        logger.error("gemini_text_error error=%s", exc)
        raise GeminiTextError(str(exc)) from exc

    required = {"category", "topic", "objective", "action_points", "tools", "market_data"}
    if missing := required - result.keys():
        raise GeminiTextError(f"Gemini response missing keys: {missing}")

    return result


# ---------------------------------------------------------------------------
# Vision client (short-video pipeline) — implemented in issue #3
# ---------------------------------------------------------------------------


async def analyze_frames(frames: list[dict]) -> dict:
    """Call Gemini Vision with base64-encoded *frames*.

    Each frame dict must have ``data`` (base64 string) and ``mime_type``.

    Returns a dict with keys: selected_frame_index, summary, links.
    Raises GeminiVisionError on failure.

    NOTE: Full implementation belongs to the short-video pipeline (issue #3).
    """
    raise NotImplementedError("analyze_frames: implemented as part of the short pipeline (issue #3)")
