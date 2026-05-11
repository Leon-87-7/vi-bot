# PRD: Video Intelligence Bot — Python Service

**Version:** 1.0  
**Status:** Ready for Implementation  
**Replaces:** n8n workflow (`The Video Intelligence Bot __prod.json`)

---

## Problem Statement

The Video Intelligence Bot currently runs as a 60-node n8n visual workflow. While functional, the workflow has become unmaintainable: debugging requires clicking through node chains, error handling is implicit in the node graph structure, state is stored in Google Sheets (no real database), and adding any new feature requires understanding the entire visual graph. The system cannot be version-controlled meaningfully, tested automatically, or reasoned about without opening the n8n editor.

---

## Solution

Replace the n8n workflow with a standalone Python service that preserves all existing behavior — short video frame analysis, long video transcript enrichment, Telegram delivery, Google Drive archival, Google Sheets logging — while adding a real database, explicit state management, structured error handling, and a clean module boundary between concerns. The service runs in Docker, receives Telegram messages via webhook, and processes videos asynchronously through a background worker queue.

---

## User Stories

1. As a user, I want to send a short video URL to the Telegram bot and receive a visual summary with extracted links, so that I can quickly understand what a short-form video contains.
2. As a user, I want to send a YouTube video URL and receive a structured analysis including category, topic, key action points, and tools mentioned, so that I can capture the value of long-form content without watching it.
3. As a user, I want to receive an immediate acknowledgment when I send a URL, so that I know the bot has accepted my request and is working on it.
4. As a user, I want the best representative frame from a short video sent as a photo in Telegram, so that I can visually identify the content at a glance.
5. As a user, I want links extracted from short video frames to appear in the Telegram message, so that I can follow up on tools and resources shown on screen.
6. As a user, I want Brave Search to surface additional related links beyond what the video showed, so that I can explore the topic further.
7. As a user, I want links extracted from YouTube video descriptions to appear in the analysis, so that I capture resources the creator explicitly shared.
8. As a user, I want all analysis reports saved to Google Drive, so that I have a persistent, searchable archive of processed videos.
9. As a user, I want a shareable Google Drive link included in the Telegram response, so that I can access the full report from any device.
10. As a user, I want every completed job logged to Google Sheets, so that I can share a human-readable history of processed videos with others.
11. As a user, I want to view job history at a `/jobs` web endpoint, so that I can browse and query processing history without opening a spreadsheet.
12. As a user, I want a retry button when Gemini Vision fails on a short video, so that I can recover from transient API failures without re-sending the URL.
13. As a user, I want a retry button when Google Drive upload fails, so that I can recover from transient Drive errors.
14. As a user, I want to see the attempt count in error messages, so that I know how many times the bot has tried to process my request.
15. As a user, I want the bot to tell me explicitly when AI enrichment failed for a long video, so that I know I'm seeing metadata-only output rather than a full analysis.
16. As a user, I want to still receive video metadata (title, channel, description links) even when Gemini fails on a long video, so that I don't lose all value from a partially failed job.
17. As a user, I want the bot to tell me when a video was already processed and show the previous Drive report link, so that I don't wait for redundant processing.
18. As a user, I want to use `/refresh <url>` to force reprocessing of a URL I've previously sent, so that I can get an updated analysis when needed.
19. As a user, I want hard errors (frame extraction failure, transcript failure) to show a clear error message rather than silently failing, so that I understand what went wrong.
20. As a user, I want the bot to be available via the same fixed ngrok URL I already use, so that I don't need to reconfigure Telegram webhook settings.
21. As a user, I want two distinct Drive folders for short and long video reports, so that my archive is organized by content type.
22. As a user, I want the Drive report filename to include the date and video title, so that I can find files in Drive without opening them.
23. As a user, I want the bot to automatically recover after a crash without losing jobs, so that I don't need to manually re-send URLs after a restart.
24. As a user, I want the bot to wait 15 seconds before retrying a failed job, so that transient API issues have time to resolve.
25. As a user, I want the bot to tell me when the processing queue is full, so that I know to try again later rather than thinking my message was ignored.

---

## Implementation Decisions

### Modules

**`db.py`** — All database operations. Uses `aiosqlite`. Manages the `jobs` table with the following schema:
- `id`: string, format `YYYYMMDD_HHMMSS_XXXX` (4 random uppercase chars)
- `chat_id`: integer
- `message_id`: integer
- `url`: text (not unique — multiple rows per URL allowed for history)
- `pipeline_type`: enum `short | long | short_transcript | long_transcript`
- `status`: enum `pending | processing | done | error`
- `attempt`: integer, starts at 0
- `error_msg`: text, nullable
- `drive_url`: text, nullable
- `processing_time_ms`: integer, nullable
- `created_at`, `updated_at`: ISO timestamps

Dedup logic uses `BEGIN EXCLUSIVE` transaction: SELECT the latest row for a URL, decide route (`recover | error | new`), INSERT if new. No UNIQUE constraint on URL. On startup, queries for any rows stuck in `processing` and re-queues them (crash recovery).

State machine legal transitions only:
- `pending → processing`
- `processing → done`
- `processing → error`
- `error → pending` (retry path only)

**`router.py`** — URL classification. Determines pipeline type from URL pattern: `youtube.com/watch` or `youtu.be` routes to `long`; all other URLs route to `short`. Validates URL format. Parses `/refresh <url>` commands, returning the URL and a `force=True` flag that bypasses dedup.

**`gemini.py`** — Gemini API clients using the `google-genai` SDK (`gemini-2.5-flash` model).
- Vision client: accepts frames as base64 inline_data (as returned by `/short_frames`), sends with the short-video prompt template (including GENERIC_ROOTS list verbatim). Returns parsed JSON: `{selected_frame_index, summary, links}`.
- Text client: accepts transcript string (truncated to 80k chars), sends with the long-video prompt template (3-category classification). Returns parsed JSON: `{category, topic, objective, action_points, tools, market_data}`.
- Both clients: raise typed exceptions on failure for pipeline error routing.

**`pipeline.py`** — Pipeline orchestration. Contains two pipeline functions: `run_short_pipeline` and `run_long_pipeline`. Each accepts a job record and executes the full pipeline for that content type, updating job status at each stage. The worker loop wraps every pipeline call in a `try/except` that unconditionally writes `status=error` on any unhandled exception.

Short pipeline:
1. Call `/short_frames` (GET, timeout 90s)
2. Check for `error` key or `frame_count == 0` → hard error (no retry)
3. Call Gemini Vision (timeout 60s) → on failure: sticker + retry button
4. If `BRAVE_API_KEY` set: call Brave Search on topic → append "Related" links section
5. Call Drive upload (2-3 retries with exponential backoff) → on failure: new sticker + retry button
6. Send Telegram: `sendPhoto` (best frame decoded from base64) + `sendMessage` (links, 4000 char limit)
7. Append to Google Sheets, update job `done`

Long pipeline:
1. `asyncio.gather(fetch_transcript(), fetch_metadata())` (timeouts: 300s / 30s)
2. Transcript `status != ok` → hard error message, mark `error`
3. Extract description links via regex, filter GENERIC_ROOTS
4. Call Gemini Text (80k char truncation) → on failure: continue with empty AI fields, include explicit error in message ("AI enrichment failed")
5. Call Drive upload (2-3 retries with exponential backoff) → on failure: new sticker + retry button
6. Send Telegram: in-progress message + final structured message
7. Append to Google Sheets, update job `done`

**`drive.py`** — Google Drive upload and Google Sheets logging. Uses a single service account (credentials mounted as volume). Drive file naming: `{job_id}_{title_slug}.md` (title_slug: lowercase, spaces → hyphens, non-alphanumeric stripped, max 60 chars). Two upload folders: short and long (folder IDs from env vars). Sheets append: one row per completed job (URL, title, pipeline type, Drive link, processing time, timestamp). Upload retries: exponential backoff, 2-3 attempts before surfacing failure.

**`telegram_bot.py`** — Telegram Bot API wrapper. Direct HTTP calls via `httpx` (no bot library). Exposes functions: `send_message`, `send_photo`, `send_sticker`, `answer_callback_query`, `register_webhook`. The webhook handler at `POST /webhook` validates the `X-Telegram-Bot-Api-Secret-Token` header. Callback query handler: answers Telegram within 10 seconds, validates state transition (`error → pending` only), then re-queues.

**`main.py`** — FastAPI application. Startup events: (1) register Telegram webhook with ngrok URL, (2) re-queue stuck `processing` jobs. Background worker loop: single worker, pulls from `asyncio.Queue(maxsize=50)`, calls `await asyncio.sleep(15)` when `attempt > 0`. `/jobs` endpoint: returns HTML table of all jobs from SQLite (job_id, URL, title, pipeline type, status, Drive link, timestamps). `/health` endpoint. When queue is full on incoming URL: reply to user "Queue is full, please try again shortly."

### Technical Decisions

- **Webhook**: FastAPI receives Telegram updates via POST `/webhook`. Telegram webhook registered on startup via `setWebhook` API call using `WEBHOOK_URL` env var (fixed ngrok domain).
- **Docker**: Service runs in Docker with `restart: always`. SQLite DB persisted via volume mount. Service account JSON mounted as volume. `host.docker.internal` used to reach the transcript server on the host machine.
- **Hard cutover**: n8n workflow is stopped before Python service starts. No parallel run.
- **Dedup**: `/refresh <url>` command creates a new job row regardless of existing records, preserving full history. Regular URL messages check the latest row for that URL.
- **GENERIC_ROOTS filter**: Lives verbatim inside the Gemini prompt text, not in Python post-processing. Gemini applies contextual judgment; string matching would not replicate this.
- **Frame passing to Gemini**: Frames from `/short_frames` are already base64 JPEG with mime_type — passed directly as `inline_data` parts to the Gemini SDK, no conversion required.
- **Brave Search**: Used for topic enhancement only (not link verification). Searches the video topic/summary and appends a "Related" section to the output. Disabled if `BRAVE_API_KEY` is absent.
- **Logging**: Standard Python `logging` module. Format: `[timestamp] LEVEL event job_id=... pipeline=... duration=...`. Structured JSON formatter can be swapped in later without changing log call sites.
- **Queue backpressure**: `asyncio.Queue(maxsize=50)`. On `asyncio.QueueFull`, reply to user with a rate-limit message instead of silently blocking.

### Environment Variables

```
TELEGRAM_BOT_TOKEN
WEBHOOK_URL
TELEGRAM_STICKER_GEMINI_FAIL
TELEGRAM_STICKER_DRIVE_FAIL
TRANSCRIPT_URL                  # default: http://host.docker.internal:5050
BRAVE_API_KEY                   # optional
GOOGLE_SERVICE_ACCOUNT_JSON     # path to credentials file in container
GOOGLE_DRIVE_FOLDER_SHORT
GOOGLE_DRIVE_FOLDER_LONG
GOOGLE_SHEETS_ID
DB_PATH                         # default: /app/data/jobs.db
PORT                            # default: 8000
NUM_WORKERS                     # default: 1
```

---

## Testing Decisions

A good test verifies external behavior only — what goes in and what comes out — without asserting on implementation details like which internal function was called or how many queries ran. Tests should remain valid through refactors as long as behavior is unchanged.

**`db.py` tests:**
- Job creation returns a correctly formatted job_id
- Dedup transaction: two concurrent inserts for the same URL produce one new job and one `recover` route (no duplicate processing)
- State transition enforcement: only legal transitions succeed; illegal transitions raise
- Startup recovery: jobs stuck in `processing` are returned for re-queuing
- `/refresh` path: new row is inserted regardless of existing done/error rows for the same URL
- `pipeline_type` column is stored and retrieved correctly for all four values

**`router.py` tests:**
- YouTube watch URLs classify as `long`
- youtu.be short URLs classify as `long`
- Instagram, TikTok, and other non-YouTube URLs classify as `short`
- Invalid URLs (non-HTTP, localhost, private IP ranges) are rejected
- `/refresh https://...` parses correctly: returns URL + `force=True`
- `/refresh` without a URL returns a validation error
- Malformed URLs do not crash the router

---

## Out of Scope

- **Carousel/Instagram carousel pipeline**: `/carousel_images` endpoint is not operational. Carousel URLs are treated as regular short videos (frame extraction path).
- **`short_transcript` and `long_transcript` pipeline types**: Future feature for key-point marking. Column exists in schema; no processing logic implemented.
- **Multi-user Google Drive OAuth**: Each user's files go to shared Drive folders owned by the service account. Individual per-user Drive authentication is not implemented.
- **Redis queue**: `asyncio.Queue` is used. Redis migration is possible but not planned.
- **Multiple workers**: `NUM_WORKERS=1`. Horizontal scaling is an env-var change but not validated.
- **Web dashboard**: The `/jobs` HTML endpoint is read-only. No create/edit/delete UI.
- **Brave Search link verification**: Only topic enhancement is implemented. Verifying extracted links against Brave is not implemented.
- **Playlist/batch processing**: One URL per Telegram message only.
- **Auto-scaling based on queue depth**: Static single worker.

---

## Further Notes

- The `/short_frames` endpoint on the transcript server (port 5050) already returns base64-encoded frames with `mime_type` fields — these map directly to the `google-genai` SDK's `inline_data` format without conversion.
- The transcript server enforces a 180-second duration limit on short videos and returns a typed `error` object on failure. Pipeline error detection must check for the `error` key in the response, not only `frame_count == 0`.
- Google Sheets was the primary state store in n8n. It is retained as a secondary reporting layer only — SQLite is the authoritative state store.
- The `pipeline_type` column future-proofs the schema for transcript-based analysis of short videos (e.g., YouTube Shorts with spoken content worth summarizing in addition to frame analysis).
- Drive file history is preserved: `/refresh` creates a new Drive file with a new job_id prefix, leaving the previous version intact.
- The two sticker file_ids (Gemini failure, Drive failure) should be distinct to give users a visual cue about which failure occurred. Obtain file_ids by forwarding any Telegram sticker to `@userinfobot`.
