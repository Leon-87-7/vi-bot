import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from config import get_settings
from db import check_dedup, create_job, get_stuck_processing_jobs, init_db
from router import classify_url
from telegram_bot import answer_callback_query, register_webhook, send_message

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    logger.info("Video Intelligence Bot starting")
    await init_db(settings.db_path)
    await register_webhook(settings)
    stuck = await get_stuck_processing_jobs(settings.db_path)
    logger.info("Stuck processing jobs to re-queue: %d", len(stuck))
    yield
    logger.info("Video Intelligence Bot shutting down")


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    settings = request.app.state.settings
    try:
        update = await request.json()

        if "message" in update and "text" in update.get("message", {}):
            message = update["message"]
            chat_id = message["chat"]["id"]
            message_id = message["message_id"]
            text = message["text"]

            try:
                route = classify_url(text)
            except ValueError as exc:
                await send_message(chat_id, str(exc), settings)
                return {"ok": True}

            if route["force"]:
                job = await create_job(
                    settings.db_path,
                    chat_id,
                    message_id,
                    route["url"],
                    route["type"],
                )
                await send_message(
                    chat_id,
                    f"✅ Job {job['id']} queued — {job['pipeline_type']}",
                    settings,
                )
                logger.info("Created job %s (force refresh)", job["id"])
            else:
                dedup = await check_dedup(settings.db_path, route["url"])
                if dedup["route"] == "recover":
                    existing = dedup["job"]
                    drive_url = existing.get("drive_url")
                    if drive_url:
                        await send_message(
                            chat_id,
                            f"Already done: {drive_url}",
                            settings,
                        )
                    else:
                        await send_message(
                            chat_id,
                            f"Job {existing['id']} is already in progress.",
                            settings,
                        )
                else:
                    job = await create_job(
                        settings.db_path,
                        chat_id,
                        message_id,
                        route["url"],
                        route["type"],
                    )
                    await send_message(
                        chat_id,
                        f"✅ Job {job['id']} queued — {job['pipeline_type']}",
                        settings,
                    )
                    logger.info("Created job %s", job["id"])

        elif "callback_query" in update:
            cq = update["callback_query"]
            await answer_callback_query(cq["id"], settings)

    except Exception:
        logger.exception("Unhandled error in /webhook")

    return {"ok": True}
