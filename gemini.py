import asyncio
import base64
import binascii
import logging

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """\
You are analyzing frames extracted from a short-form video.
The frames are attached in order. Frame indices and their timestamps:
{frame_list}

**Task 1 — Select the Best Frame & Summarize**
Choose the SINGLE frame that best represents the content of this video.
Prioritize frames showing:
- Any URL, link, or website address
- Code, terminal output, or a README
- A tool, product, or service being demonstrated
- A list of resources, repos, or libraries

Also write a concise summary of what this video is about — 2-3 sentences, technical tone, no fluff.
Focus on: what the tool/project does, what problem it solves, and why a developer would care.

**Task 2 — Extract All Visible Links**
Look through ALL frames carefully and identify every unique link, URL, or named resource that is:
- Shown as a full URL (e.g. github.com/owner/repo, example.com, docs.something.io)
- Displayed as a domain or path the viewer is meant to visit
- A named tool, product, or service that clearly has an associated website

Rules:
- Each unique URL must appear ONCE in your output — do not repeat the same URL even if it appears in multiple frames
- For GitHub repos, include only the main repo URL (e.g. https://github.com/owner/repo) — do not list the same repo multiple times or include sub-pages of the same repo
- Only include links you are confident about — do not hallucinate

For each unique link found:
- Provide the name as shown on screen
- Provide the full URL. If the protocol is missing on screen, prepend https:// automatically to create a valid link.
- Write a one-sentence description of what it is

Produce your response as a Markdown document with these sections:

## Summary
<2-3 sentence technical summary of the video>

## Links
- [name](url) — one sentence description

(one entry per unique link found; omit this section if no links were found)"""


async def analyse_short(frames: list[dict], url: str, client: genai.Client) -> str:
    if not frames:
        raise ValueError("frames list is empty")
    for i, f in enumerate(frames):
        missing = [
            k for k in ("mime_type", "base64", "index", "timestamp_s") if k not in f
        ]
        if missing:
            raise ValueError(f"Frame {i} missing required keys: {missing}")

    frame_list = ", ".join(f"Frame {f['index']} = {f['timestamp_s']}s" for f in frames)
    prompt_text = _PROMPT_TEMPLATE.format(frame_list=frame_list)

    try:
        image_parts = [
            types.Part(
                inline_data=types.Blob(
                    mime_type=f["mime_type"],
                    data=base64.b64decode(f["base64"]),
                )
            )
            for f in frames
        ]
    except binascii.Error as exc:
        raise ValueError(f"Invalid base64 data in frame: {exc}") from exc

    parts = image_parts + [types.Part(text=prompt_text)]

    try:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-flash-lite",
            contents=parts,
        )
    except Exception as exc:
        logger.error("Gemini API call failed: %s", exc)
        raise

    text = getattr(response, "text", None)
    if not text:
        raise ValueError("Gemini returned an empty response")
    return text
