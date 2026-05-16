import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import get_settings
from db import check_dedup, create_job, init_db
from pipeline import start_workers
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
    try:
        await init_db(settings.db_path)
    except Exception as exc:
        raise RuntimeError("Failed to initialize DB") from exc
    try:
        await register_webhook(settings)
    except Exception as exc:
        raise RuntimeError("Failed to register webhook") from exc
    try:
        await start_workers(app, settings)
    except Exception as exc:
        raise RuntimeError("Failed to start workers") from exc
    yield
    tasks = getattr(app.state, "worker_tasks", [])
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Video Intelligence Bot shutting down")


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    settings = request.app.state.settings
    if settings.telegram_webhook_secret:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token != settings.telegram_webhook_secret:
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    try:
        update = await request.json()

        if "message" in update and "text" in update.get("message", {}):
            message = update["message"]
            chat = message.get("chat")
            if not isinstance(chat, dict) or "id" not in chat:
                return {"ok": True}
            chat_id = chat["id"]
            message_id = message.get("message_id")
            if message_id is None:
                return {"ok": True}
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
                try:
                    request.app.state.queue.put_nowait(job)
                except asyncio.QueueFull:
                    await send_message(chat_id, "Bot is busy, try again later", settings)
                    return {"ok": True}
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
                    try:
                        request.app.state.queue.put_nowait(job)
                    except asyncio.QueueFull:
                        await send_message(chat_id, "Bot is busy, try again later", settings)
                        return {"ok": True}
                    await send_message(
                        chat_id,
                        f"✅ Job {job['id']} queued — {job['pipeline_type']}",
                        settings,
                    )
                    logger.info("Created job %s", job["id"])

        elif "callback_query" in update:
            cq = update.get("callback_query")
            if isinstance(cq, dict) and cq.get("id"):
                await answer_callback_query(cq["id"], settings)

    except Exception:
        logger.exception("Unhandled error in /webhook")

    return {"ok": True}