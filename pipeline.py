import asyncio
import base64
from datetime import datetime, timezone
import html as _html
import logging
import pathlib
import re
import time

import httpx
from google import genai
from google.oauth2 import service_account

from config import Settings
from db import get_stuck_processing_jobs, transition_status, update_job
from drive import append_to_sheet, build_services, slugify, upload_to_drive
from gemini import analyse_short
from telegram_bot import send_message, send_photo, send_sticker

logger = logging.getLogger(__name__)

_GENAI_SCOPES = ["https://www.googleapis.com/auth/generative-language"]


_MD_LINK_RE = re.compile(r"- \[(.+?)\]\((.+?)\)(?:\s*[—–-]\s*(.+))?")
_PLAIN_LINK_RE = re.compile(r"- (.+?)\s+(https?://\S+?)(?:\s*[—–-]\s*(.+))?$")


def _parse_link_line(line: str) -> tuple[str, str, str] | None:
    if m := _MD_LINK_RE.match(line):
        return (m.group(1), m.group(2), (m.group(3) or "").strip())
    if m := _PLAIN_LINK_RE.match(line):
        url = m.group(2).rstrip(".,;:)")
        return (m.group(1).strip(), url, (m.group(3) or "").strip())
    return None


def _parse_report(report: str) -> dict:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in report.splitlines():
        s = line.strip()
        if s.startswith("## "):
            current = s[3:].strip()
            sections[current] = []
        elif current is not None and s:
            sections[current].append(s)
    summary = " ".join(sections.get("Summary", []))
    links = [r for line in sections.get("Links", []) if (r := _parse_link_line(line))]
    related = [
        (m.group(1), m.group(2))
        for line in sections.get("Related", [])
        if (m := re.match(r"- \[(.+?)\]\((.+?)\)", line))
    ]
    return {"summary": summary, "links": links, "related": related}


def _format_success_message(parsed: dict, drive_url: str, filename: str) -> str:
    e = _html.escape
    summary = parsed["summary"]
    links = parsed["links"]
    related = parsed["related"]

    parts = ["✅ <b>Done!</b>"]
    if summary:
        parts += ["", f"<i>{e(summary)}</i>"]

    if links:
        parts += ["", "🔗 <b>Links Found:</b>"]
        for name, url, desc in links:
            entry = f"• <b>{e(name)}</b>"
            if desc:
                entry += f" — {e(desc)}"
            parts += ["", entry, f'🔗 <a href="{url}">{e(url)}</a>']
        parts += ["", "---", "🔗 <b>Quick Links:</b>"]
        parts += [f'<a href="{url}">{e(url)}</a>' for _, url, _ in links]

    if related:
        parts += ["", "🌐 <b>Related:</b>"]
        parts += [f'• <a href="{url}">{e(title)}</a>' for title, url in related]

    parts += ["", f'📄 <a href="{drive_url}">{e(filename)}</a>']
    return "\n".join(parts)


def _build_tools_message(links: list[tuple[str, str, str]]) -> str:
    if not links:
        return ""
    parts = ["🔗 *Links Found:*"]
    for name, url, desc in links:
        entry = f"\n• *{name}*"
        if desc:
            entry += f" — {desc}"
        parts.append(entry)
        parts.append(f"  🔗 {url}")
    parts += ["\n---", "🔗 *Quick Links:*"] + [url for _, url, _ in links]
    return "\n".join(parts)


async def run_short_job(job: dict, settings: Settings, gemini_client, drive_svc, sheets_svc) -> None:
    start_time = time.monotonic()
    await transition_status(settings.db_path, job["id"], "pending", "processing")

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)) as http:
        resp = await http.get(
            f"{settings.transcript_url}/short_frames",
            params={"url": job["url"]},
        )
        resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"Transcript server error: {body['error']}")
    frames = body["frames"]

    report = await analyse_short(frames, job["url"], gemini_client)

    try:
        debug_dir = pathlib.Path(settings.db_path).parent / "reports"
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"{job['id']}.md").write_text(report, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not save debug report for %s: %s", job["id"], exc)

    _topic = next(
        (line.strip() for line in report.splitlines() if line.strip() and not line.lstrip().startswith("#")),
        "video",
    )

    if settings.brave_api_key:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as http:
                brave_resp = await http.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": _topic, "count": 5},
                    headers={"X-Subscription-Token": settings.brave_api_key},
                )
            brave_resp.raise_for_status()
            results = brave_resp.json().get("web", {}).get("results", [])
            related_lines = [
                f"- [{r['title']}]({r['url']})" for r in results if "title" in r and "url" in r
            ]
            if related_lines:
                report += "\n\n## Related\n" + "\n".join(related_lines)
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            logger.warning("Brave enrichment skipped: %s", exc)

    title_slug = slugify(_topic)
    filename = f"{job['id']}_{title_slug}.md"

    drive_url = await asyncio.to_thread(
        upload_to_drive, drive_svc, settings.google_drive_folder_short, filename, report
    )

    parsed = _parse_report(report)
    links_parsed = parsed["links"]
    link_urls = [url for _, url, _ in links_parsed]
    tools_msg = _build_tools_message(links_parsed)
    links_str = "\n".join(link_urls)
    tools_count = len(link_urls)
    platform = body.get("platform", "")
    title = body.get("title", "")
    duration_s = body.get("duration_s")
    frame_count = len(frames)
    processed_at = datetime.now(timezone.utc).isoformat()

    await asyncio.to_thread(
        append_to_sheet,
        sheets_svc,
        settings.google_sheets_id_short,
        [
            job["id"],
            job["url"],
            job["chat_id"],
            "done",
            platform,
            title,
            duration_s,
            frame_count,
            0,
            tools_msg,
            links_str,
            tools_count,
            job["created_at"],
            processed_at,
            "",
        ],
    )

    t_ms = int((time.monotonic() - start_time) * 1000)
    await update_job(
        settings.db_path, job["id"],
        status="done", drive_url=drive_url, processing_time_ms=t_ms,
        platform=platform, title=title, duration_s=duration_s,
        frame_count=frame_count, best_frame_index=0,
        tools_message=tools_msg, links=links_str, tools_count=tools_count,
    )
    try:
        photo_bytes = base64.b64decode(frames[0]["base64"])
        await send_photo(job["chat_id"], photo_bytes, settings)
        msg = _format_success_message(parsed, drive_url, filename)
        await send_message(job["chat_id"], msg, settings, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Notification failed for job %s: %s", job["id"], exc)


async def worker(queue: asyncio.Queue, settings: Settings, gemini_client, drive_svc, sheets_svc) -> None:
    while True:
        job = await queue.get()
        attempt = job.get("attempt", 0)
        if attempt > 0:
            await asyncio.sleep(15)
        try:
            if job["pipeline_type"] == "short":
                await run_short_job(job, settings, gemini_client, drive_svc, sheets_svc)
        except Exception as exc:
            logger.exception("Job %s failed: %s", job["id"], exc)
            try:
                await update_job(settings.db_path, job["id"], status="error", error_msg=str(exc))
                await update_job(settings.db_path, job["id"], attempt=attempt + 1)
                await send_sticker(job["chat_id"], settings.telegram_sticker_gemini_fail, settings)
                await send_message(job["chat_id"], f"❌ Job `{job['id']}` failed:\n{exc}", settings)
            except Exception:
                logger.exception("Failed to record error for job %s", job["id"])
        finally:
            queue.task_done()


async def start_workers(app, settings: Settings) -> None:
    gemini_client = genai.Client(api_key=settings.gemini_api_key)

    drive_svc, sheets_svc = build_services(
        settings.google_service_account_json,
        settings.google_oauth_client_id,
        settings.google_oauth_client_secret,
        settings.google_oauth_refresh_token,
    )

    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    app.state.queue = queue

    worker_tasks = [
        asyncio.create_task(worker(queue, settings, gemini_client, drive_svc, sheets_svc))
        for _ in range(settings.num_workers)
    ]
    app.state.worker_tasks = worker_tasks

    stuck = await get_stuck_processing_jobs(settings.db_path)
    for stuck_job in stuck:
        await queue.put(stuck_job)
    if stuck:
        logger.info("Re-queued %d stuck processing jobs", len(stuck))