"""Pipeline orchestration for the Video Intelligence Bot."""

import asyncio
import logging
import re
import time
from urllib.parse import urlparse

import httpx

from config import get_settings
from db import update_job, transition_status
from drive import upload_report, append_to_sheets, DriveUploadError
from gemini import analyze_transcript, GeminiTextError, GENERIC_ROOTS
from telegram_bot import send_message, send_sticker

logger = logging.getLogger(__name__)

_TRANSCRIPT_TIMEOUT = 300.0
_METADATA_TIMEOUT = 30.0
_URL_RE = re.compile(r"https?://[^\s\)\]>\"<,]+")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _fetch_transcript(url: str, base_url: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{base_url}/transcript",
            params={"url": url},
            timeout=_TRANSCRIPT_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()


async def _fetch_metadata(url: str, base_url: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{base_url}/metadata",
            params={"url": url},
            timeout=_METADATA_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()


def _extract_description_links(description: str) -> list[str]:
    """Return non-generic URLs found in *description*, deduped, order preserved."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in _URL_RE.findall(description):
        link = raw.rstrip(".,;!?)")
        if link in seen:
            continue
        seen.add(link)
        host = (urlparse(link).hostname or "").removeprefix("www.")
        if not any(host == r or host.endswith("." + r) for r in GENERIC_ROOTS):
            result.append(link)
    return result


def _make_drive_filename(job_id: str, title: str) -> str:
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = slug[:60].rstrip("-")
    return f"{job_id}_{slug}.md"


def _retry_markup(job_id: str) -> dict:
    return {"inline_keyboard": [[{"text": "🔄 Retry", "callback_data": f"retry:{job_id}"}]]}


def _build_drive_report(
    job: dict,
    title: str,
    channel: str,
    url: str,
    ai: dict,
    desc_links: list[str],
    ai_error: str | None,
) -> str:
    lines = [f"# {title}\n"]
    lines.append(f"**Channel:** {channel}  ")
    lines.append(f"**URL:** {url}  ")
    lines.append(f"**Job ID:** {job['id']}  \n")

    if ai_error:
        lines.append(f"> ⚠️ AI enrichment failed: {ai_error}\n")

    if ai.get("category"):
        lines.append(f"## Category\n{ai['category']}\n")
    if ai.get("topic"):
        lines.append(f"## Topic\n{ai['topic']}\n")
    if ai.get("objective"):
        lines.append(f"## Objective\n{ai['objective']}\n")

    action_points = ai.get("action_points") or []
    if action_points:
        lines.append("## Action Points")
        lines.extend(f"- {p}" for p in action_points)
        lines.append("")

    tools = ai.get("tools") or []
    if tools:
        lines.append("## Tools")
        lines.extend(f"- {t}" for t in tools)
        lines.append("")

    if ai.get("market_data"):
        lines.append(f"## Market Data\n{ai['market_data']}\n")

    if desc_links:
        lines.append("## From Description")
        lines.extend(f"- {lnk}" for lnk in desc_links)
        lines.append("")

    return "\n".join(lines)


def _build_telegram_message(
    title: str,
    ai: dict,
    desc_links: list[str],
    drive_url: str,
    ai_error: str | None,
) -> str:
    parts: list[str] = []

    if ai_error:
        parts.append(f"⚠️ *AI enrichment failed:* {ai_error}\n")

    parts.append(f"📹 *{title}*\n")

    if ai.get("category"):
        parts.append(f"📂 *Category:* {ai['category']}")
    if ai.get("topic"):
        parts.append(f"🎯 *Topic:* {ai['topic']}")
    if ai.get("objective"):
        parts.append(f"📌 *Objective:* {ai['objective']}\n")

    action_points = ai.get("action_points") or []
    if action_points:
        parts.append("✅ *Key Actions:*")
        parts.extend(f"• {p}" for p in action_points)
        parts.append("")

    tools = ai.get("tools") or []
    if tools:
        parts.append("🛠 *Tools:*")
        parts.extend(f"• {t}" for t in tools)
        parts.append("")

    if ai.get("market_data"):
        parts.append(f"📊 *Market Context:*\n{ai['market_data']}\n")

    if desc_links:
        parts.append("🔗 *From Description:*")
        parts.extend(f"• {lnk}" for lnk in desc_links)
        parts.append("")

    parts.append(f"📁 [View Report]({drive_url})")

    msg = "\n".join(parts)
    return msg[:4000]


# ---------------------------------------------------------------------------
# Long-video pipeline
# ---------------------------------------------------------------------------


async def run_long_pipeline(job: dict) -> None:
    """Execute the long-form video analysis pipeline for *job*.

    Precondition: job status is already 'processing' (set by the worker).
    """
    settings = get_settings()
    db_path = settings.db_path
    chat_id = job["chat_id"]
    job_id = job["id"]
    url = job["url"]
    start = time.monotonic()

    logger.info("long_pipeline_start job_id=%s url=%s", job_id, url)

    # 1. In-progress acknowledgement
    await send_message(chat_id, "⏳ Analyzing video — this may take a few minutes…")

    # 2. Fetch transcript + metadata in parallel
    try:
        transcript_data, metadata = await asyncio.gather(
            _fetch_transcript(url, settings.transcript_url),
            _fetch_metadata(url, settings.transcript_url),
        )
    except Exception as exc:
        logger.error("long_pipeline fetch_error job_id=%s error=%s", job_id, exc)
        await send_message(chat_id, f"❌ Could not fetch video data: {exc}")
        await update_job(db_path, job_id, error_msg=str(exc))
        await transition_status(db_path, job_id, "processing", "error")
        return

    # 3. Hard error if transcript unavailable
    if transcript_data.get("status") != "ok":
        err = transcript_data.get("message") or "transcript service error"
        logger.warning("long_pipeline transcript_error job_id=%s msg=%s", job_id, err)
        await send_message(chat_id, f"❌ Could not get transcript: {err}")
        await update_job(db_path, job_id, error_msg=err)
        await transition_status(db_path, job_id, "processing", "error")
        return

    # 4. Extract metadata fields (tolerate metadata failure gracefully)
    meta_ok = isinstance(metadata, dict) and metadata.get("status") == "ok"
    title = metadata.get("title", "Unknown Title") if meta_ok else "Unknown Title"
    channel = metadata.get("channel", "") if meta_ok else ""
    description = metadata.get("description", "") if meta_ok else ""
    desc_links = _extract_description_links(description)

    # 5. Gemini Text — recover on failure
    ai_error: str | None = None
    ai_result: dict = {
        "category": "",
        "topic": "",
        "objective": "",
        "action_points": [],
        "tools": [],
        "market_data": "",
    }
    try:
        ai_result = await analyze_transcript(transcript_data["transcript"])
    except GeminiTextError as exc:
        ai_error = str(exc)
        logger.warning("long_pipeline gemini_failure job_id=%s error=%s", job_id, exc)

    # 6. Build Drive report
    report_md = _build_drive_report(job, title, channel, url, ai_result, desc_links, ai_error)
    filename = _make_drive_filename(job_id, title)

    # 7. Drive upload (DriveUploadError → sticker + retry button)
    try:
        drive_url = await upload_report(
            content=report_md,
            filename=filename,
            folder_id=settings.google_drive_folder_long,
        )
    except (DriveUploadError, Exception) as exc:
        logger.error("long_pipeline drive_error job_id=%s error=%s", job_id, exc)
        await send_sticker(chat_id, settings.telegram_sticker_drive_fail)
        await send_message(
            chat_id,
            f"❌ Drive upload failed (attempt {job['attempt'] + 1}): {exc}",
            reply_markup=_retry_markup(job_id),
        )
        await update_job(db_path, job_id, error_msg=str(exc))
        await transition_status(db_path, job_id, "processing", "error")
        return

    # 8. Final Telegram message
    msg = _build_telegram_message(title, ai_result, desc_links, drive_url, ai_error)
    await send_message(chat_id, msg)

    # 9. Sheets log + mark done (job is done even when Gemini failed)
    processing_ms = int((time.monotonic() - start) * 1000)
    try:
        await append_to_sheets(
            job,
            title,
            drive_url,
            sheets_id=settings.google_sheets_id_long,
        )
    except Exception as exc:
        logger.warning("long_pipeline sheets_error job_id=%s error=%s", job_id, exc)

    await update_job(db_path, job_id, drive_url=drive_url, processing_time_ms=processing_ms)
    await transition_status(db_path, job_id, "processing", "done")
    logger.info("long_pipeline_done job_id=%s ms=%d", job_id, processing_ms)


# ---------------------------------------------------------------------------
# Short-video pipeline — implemented in issue #3
# ---------------------------------------------------------------------------


async def run_short_pipeline(job: dict) -> None:
    """Execute the short-form video analysis pipeline for *job*.

    NOTE: Full implementation belongs to the short-video pipeline (issue #3).
    """
    raise NotImplementedError("run_short_pipeline: implemented in issue #3")
