import logging

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _url(token: str, method: str) -> str:
    return TELEGRAM_API.format(token=token, method=method)


async def register_webhook(settings) -> None:
    url = _url(settings.telegram_bot_token, "setWebhook")
    payload = {"url": f"{settings.webhook_url}/webhook"}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload)
    if resp.is_success:
        logger.info("Telegram webhook registered: %s", payload["url"])
    else:
        logger.error("Failed to register webhook: %s %s", resp.status_code, resp.text)
    resp.raise_for_status()


async def send_message(chat_id: int, text: str, settings) -> None:
    url = _url(settings.telegram_bot_token, "sendMessage")
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={"chat_id": chat_id, "text": text})
    resp.raise_for_status()


async def send_sticker(chat_id: int, sticker_file_id: str, settings) -> None:
    url = _url(settings.telegram_bot_token, "sendSticker")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url, json={"chat_id": chat_id, "sticker": sticker_file_id}
        )
    resp.raise_for_status()


async def answer_callback_query(
    callback_query_id: str, settings, text: str = ""
) -> None:
    url = _url(settings.telegram_bot_token, "answerCallbackQuery")
    async with httpx.AsyncClient() as client:
        await client.post(
            url, json={"callback_query_id": callback_query_id, "text": text}
        )
