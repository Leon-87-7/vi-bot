import asyncio
import logging
import time

import httpx
from google import genai
from google.oauth2 import service_account

from config import Settings
from db import get_stuck_processing_jobs, transition_status, update_job
from drive import append_to_sheet, build_services, slugify, upload_to_drive
from gemini import analyse_short
from telegram_bot import send_message, send_sticker

logger = logging.getLogger(__name__)

_GENAI_SCOPES = ["https://www.googleapis.com/auth/generative-language"]


async def run_short_job(job: dict, settings: Settings, gemini_client, drive_svc, sheets_svc) -> None:
    start_time = time.monotonic()
    await transition_status(settings.db_path, job["id"], "pending", "processing")

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as http:
        resp = await http.get(
            f"{settings.transcript_url}/short_frames",
            params={"url": job["url"]},
        )
        resp.raise_for_status()
    frames = resp.json()

    report = await analyse_short(frames, job["url"], gemini_client)

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
    await asyncio.to_thread(
        append_to_sheet,
        sheets_svc,
        settings.google_sheets_id_short,
        [job["id"], job["url"], drive_url, job["pipeline_type"], "done"],
    )

    t_ms = int((time.monotonic() - start_time) * 1000)
    await update_job(settings.db_path, job["id"], status="done", drive_url=drive_url, processing_time_ms=t_ms)
    try:
        await send_message(job["chat_id"], f"✅ Done! [{filename}]({drive_url})", settings)
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
            except Exception:
                logger.exception("Failed to record error for job %s", job["id"])
        finally:
            queue.task_done()


async def start_workers(app, settings: Settings) -> None:
    creds = service_account.Credentials.from_service_account_file(
        settings.google_service_account_json,
        scopes=_GENAI_SCOPES,
    )
    gemini_client = genai.Client(credentials=creds)

    drive_svc, sheets_svc = build_services(settings.google_service_account_json)

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