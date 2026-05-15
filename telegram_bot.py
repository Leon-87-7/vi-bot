"""Telegram Bot API wrapper (direct httpx calls).

Full implementation is delivered in issue #3.  These stubs define the
interface consumed by pipeline.py so the module can be imported and tested
with mocks today.
"""

from typing import Any


async def send_message(
    chat_id: int,
    text: str,
    *,
    reply_markup: dict | None = None,
    parse_mode: str = "Markdown",
) -> dict:
    """Send a text message to *chat_id*.  Returns the Telegram Message object."""
    raise NotImplementedError("send_message: implemented in issue #3")


async def send_photo(
    chat_id: int,
    photo: bytes,
    *,
    caption: str = "",
    parse_mode: str = "Markdown",
) -> dict:
    """Send *photo* bytes to *chat_id*.  Returns the Telegram Message object."""
    raise NotImplementedError("send_photo: implemented in issue #3")


async def send_sticker(chat_id: int, sticker_id: str) -> dict:
    """Send a sticker to *chat_id*.  Returns the Telegram Message object."""
    raise NotImplementedError("send_sticker: implemented in issue #3")


async def answer_callback_query(
    callback_query_id: str,
    text: str = "",
) -> dict:
    """Acknowledge a Telegram callback query within the 10-second window."""
    raise NotImplementedError("answer_callback_query: implemented in issue #3")


async def register_webhook(webhook_url: str, secret_token: str) -> None:
    """Register *webhook_url* with Telegram's setWebhook API."""
    raise NotImplementedError("register_webhook: implemented in issue #3")
