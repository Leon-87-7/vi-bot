from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from db import create_job, init_db, update_job
from telegram_bot import register_webhook, send_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(tmp_path):
    s = MagicMock()
    s.telegram_bot_token = "test-token"
    s.webhook_url = "https://example.ngrok.io"
    s.db_path = str(tmp_path / "test.db")
    return s


def _yt_update(chat_id=1, message_id=1, text="https://youtu.be/abc"):
    return {
        "update_id": 1,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id},
            "text": text,
        },
    }


# ---------------------------------------------------------------------------
# 1. register_webhook sends correct payload to correct URL
# ---------------------------------------------------------------------------


async def test_register_webhook_correct_payload():
    settings = MagicMock()
    settings.telegram_bot_token = "tok123"
    settings.webhook_url = "https://example.ngrok.io"

    mock_response = MagicMock()
    mock_response.is_success = True
    mock_response.raise_for_status = MagicMock()

    posted = {}

    async def fake_post(url, **kwargs):
        posted["url"] = url
        posted["json"] = kwargs.get("json")
        return mock_response

    with patch("telegram_bot.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = fake_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await register_webhook(settings)

    assert posted["url"] == "https://api.telegram.org/bottok123/setWebhook"
    assert posted["json"] == {"url": "https://example.ngrok.io/webhook"}


# ---------------------------------------------------------------------------
# 2. send_message sends correct payload
# ---------------------------------------------------------------------------


async def test_send_message_correct_payload():
    settings = MagicMock()
    settings.telegram_bot_token = "tok123"

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    posted = {}

    async def fake_post(url, **kwargs):
        posted["url"] = url
        posted["json"] = kwargs.get("json")
        return mock_response

    with patch("telegram_bot.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = fake_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await send_message(42, "hello", settings)

    assert posted["url"] == "https://api.telegram.org/bottok123/sendMessage"
    assert posted["json"]["chat_id"] == 42
    assert posted["json"]["text"] == "hello"


# ---------------------------------------------------------------------------
# Shared async client factory
#
# We set app.state.settings directly after reload to avoid relying on
# lifespan startup (ASGITransport does not fire lifespan events).
# register_webhook and send_message are patched at module level so the
# patches remain active for the duration of the async client session.
# ---------------------------------------------------------------------------


async def _post_webhook(app, payload):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhook", json=payload)


# ---------------------------------------------------------------------------
# 3. /webhook: valid YouTube URL → job created, 200 returned
# ---------------------------------------------------------------------------


async def test_webhook_valid_youtube_url_creates_job(tmp_path):
    import importlib
    import main as main_module

    settings = _make_settings(tmp_path)
    await init_db(settings.db_path)

    importlib.reload(main_module)
    main_module.app.state.settings = settings

    sent = []

    async def fake_send(chat_id, text, s):
        sent.append((chat_id, text))

    with patch("main.send_message", side_effect=fake_send):
        resp = await _post_webhook(
            main_module.app, _yt_update(text="https://youtu.be/abc")
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert any("queued" in msg for _, msg in sent)


# ---------------------------------------------------------------------------
# 4. /webhook: invalid URL → error message sent, 200 returned (no crash)
# ---------------------------------------------------------------------------


async def test_webhook_invalid_url_sends_error_no_crash(tmp_path):
    import importlib
    import main as main_module

    settings = _make_settings(tmp_path)
    await init_db(settings.db_path)

    importlib.reload(main_module)
    main_module.app.state.settings = settings

    sent = []

    async def fake_send(chat_id, text, s):
        sent.append((chat_id, text))

    with patch("main.send_message", side_effect=fake_send):
        resp = await _post_webhook(main_module.app, _yt_update(text="not-a-url"))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert len(sent) > 0


# ---------------------------------------------------------------------------
# 5. /webhook: dedup "recover" → returns existing info, no new job created
# ---------------------------------------------------------------------------


async def test_webhook_dedup_recover_returns_existing_no_new_job(tmp_path):
    import importlib
    import main as main_module
    from db import check_dedup

    settings = _make_settings(tmp_path)
    await init_db(settings.db_path)

    url = "https://youtu.be/existingvideo"
    existing = await create_job(settings.db_path, 1, 1, url, "long")
    await update_job(
        settings.db_path,
        existing["id"],
        status="done",
        drive_url="https://drive.google.com/file/existing",
    )

    importlib.reload(main_module)
    main_module.app.state.settings = settings

    sent = []

    async def fake_send(chat_id, text, s):
        sent.append((chat_id, text))

    with patch("main.send_message", side_effect=fake_send):
        resp = await _post_webhook(main_module.app, _yt_update(text=url))

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert any("drive.google.com/file/existing" in msg for _, msg in sent)

    dedup = await check_dedup(settings.db_path, url)
    assert dedup["job"]["id"] == existing["id"]
