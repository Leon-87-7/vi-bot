# Video Intelligence Bot - Product Requirements Document (PRD)

## Document Information
- **Version:** 1.0
- **Last Updated:** May 11, 2026
- **Author:** Leon Eidelman (Technical Architecture)
- **Status:** Draft for Implementation
- **Project Type:** Portfolio + Personal Tool

- **Relevant Files:** 
- "C:\Users\leone\Desktop\codeKitchen\yt_scrap\The Video Intelligence Bot __prod.json"
- "C:\Users\leone\Desktop\codeKitchen\yt_scrap\transcript_server.py"
- "C:\Users\leone\n8n-local\docker-compose.yml"
---

## 1. Executive Summary

### 1.1 Problem Statement
The current n8n-based video intelligence workflow has become unmaintainable due to:
- **Mixed concerns** across 60+ nodes handling Telegram bot UX, job state, frame analysis, transcript extraction, AI enrichment, and storage
- **Google Sheets as transactional database** causing latency and complexity
- **Repetitive field mapping** and status updates scattered across multiple branches
- **Poor observability** - difficult debugging in visual workflow canvas
- **Docker networking inconsistencies** between hardcoded IPs (`10.0.0.4`) and container aliases (`host.docker.internal`)

### 1.2 Proposed Solution
Replace the n8n workflow with a standalone Python service (FastAPI + SQLite + Redis) that:
- Separates concerns into clear architectural layers
- Uses proper database for job state management
- Provides structured logging and observability
- Maintains Telegram bot integration with improved UX
- Reduces codebase from 60+ visual nodes to ~500 lines of maintainable Python

### 1.3 Success Metrics
- **Maintainability:** Time to implement feature changes reduced by 70%
- **Reliability:** Job failure rate < 2%
- **Performance:** Average job processing time < 30 seconds for short videos, < 90 seconds for long videos
- **Observability:** All jobs logged with structured data, queryable error analytics
- **Developer Experience:** New developer can understand architecture in < 1 hour

---

## 2. System Architecture

### 2.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     TELEGRAM BOT LAYER                       │
│  (Webhook receiver, message sender, callback handler)        │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│                      API LAYER (FastAPI)                     │
│  • /webhook - Receive Telegram messages                      │
│  • /callback - Handle retry button clicks                    │
│  • /health - Service health check                            │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│                   JOB MANAGEMENT LAYER                       │
│  • SQLite database (jobs table)                              │
│  • Job CRUD operations                                       │
│  • Status state machine                                      │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│                  ASYNC PROCESSING LAYER                      │
│  • Redis Queue (or asyncio.Queue)                            │
│  • Background worker processes                               │
└─────┬──────────────────────────────────────────────┬────────┘
      │                                              │
┌─────▼─────────────────────┐    ┌─────────────────▼─────────┐
│   SHORT VIDEO PIPELINE    │    │   LONG VIDEO PIPELINE      │
│ • Frame extraction        │    │ • Transcript extraction    │
│ • Gemini Vision analysis  │    │ • Gemini Text enrichment   │
│ • Brave Search (optional) │    │ • Metadata extraction      │
│ • Markdown generation     │    │ • Markdown generation      │
└─────┬─────────────────────┘    └─────────────────┬─────────┘
      │                                            │
      └──────────────┬─────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│                      OUTPUT LAYER                            │
│  • Google Drive upload (markdown storage)                    │
│  • Google Sheets logging (reporting only)                    │
│  • Telegram response formatting & sending                    │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 Component Specifications

#### 2.2.1 Telegram Bot Layer
**Technology:** `httpx` for direct Telegram API calls (no wrapper library)

**Responsibilities:**
- Receive webhook POST from Telegram servers
- Parse incoming message for video URLs
- Send immediate acknowledgment to user
- Handle callback queries (retry button clicks)
- Format and send completion/error messages with inline keyboards

**Key Interfaces:**
```python
async def handle_webhook(update: TelegramUpdate) -> Response:
    """Process incoming Telegram webhook"""
    
async def send_message(chat_id: int, text: str, reply_markup: Optional[dict]) -> None:
    """Send message to Telegram user"""
    
async def handle_callback_query(callback_query: CallbackQuery) -> None:
    """Handle inline button clicks (retry actions)"""
```

**Webhook Security:**
```python
# Validate incoming webhooks using secret token
def validate_telegram_request(request: Request) -> bool:
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    return secret == os.getenv("TELEGRAM_WEBHOOK_SECRET")
```

#### 2.2.2 API Layer
**Technology:** FastAPI with `uvicorn` ASGI server

**Endpoints:**
| Endpoint | Method | Purpose | Auth |
|----------|--------|---------|------|
| `/webhook` | POST | Receive Telegram updates | Telegram token validation |
| `/callback` | POST | Handle retry button clicks | Telegram token validation |
| `/health` | GET | Service health check | Public |
| `/jobs/{job_id}` | GET | Query job status (internal) | API key |

**Request/Response Flow:**
1. Telegram sends POST to `/webhook`
2. API validates URL format and content type
3. Creates job record in database with status `pending`
4. Adds job to processing queue
5. Returns HTTP 200 to Telegram immediately
6. Worker processes job asynchronously
7. On completion, sends result via Telegram API

**Example Webhook Handler:**
```python
@app.post("/webhook")
async def webhook(request: Request):
    # Validate request
    if not validate_telegram_request(request):
        raise HTTPException(status_code=403, detail="Invalid token")
    
    # Parse Telegram update
    update = await request.json()
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")
    
    # Validate URL
    if not is_valid_video_url(text):
        await send_message(chat_id, "❌ Invalid video URL")
        return {"ok": True}
    
    # Detect content type
    content_type = detect_content_type(text)
    
    # Create job
    job_id = await create_job(
        chat_id=chat_id,
        url=text,
        content_type=content_type,
        message_id=message.get("message_id")
    )
    
    # Queue for processing
    await queue.put(job_id)
    
    # Send acknowledgment
    await send_message(
        chat_id=chat_id,
        text="📥 Received! Processing your video...\nYou'll be notified when complete."
    )
    
    return {"ok": True}
```

#### 2.2.3 Job Management Layer
**Technology:** SQLite (or PostgreSQL for production scale)

**Database Schema:**
```sql
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,                -- UUID v4
    chat_id INTEGER NOT NULL,           -- Telegram chat ID
    message_id INTEGER,                 -- Original message ID
    url TEXT NOT NULL,                  -- Source video URL
    content_type TEXT NOT NULL,         -- 'short' | 'long'
    status TEXT NOT NULL DEFAULT 'pending', -- State machine status
    attempt INTEGER DEFAULT 1,          -- Retry counter
    error_msg TEXT,                     -- Last error message
    drive_url TEXT,                     -- Google Drive markdown URL
    sheets_row_id TEXT,                 -- Google Sheets row reference
    processing_time_ms INTEGER,         -- Performance metric
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    
    CHECK(content_type IN ('short', 'long')),
    CHECK(status IN ('pending', 'processing', 'complete', 'error', 'cancelled'))
);

CREATE INDEX idx_status_created ON jobs(status, created_at);
CREATE INDEX idx_chat_id ON jobs(chat_id);
CREATE INDEX idx_url_hash ON jobs(url); -- For deduplication
```

**State Machine:**
```
pending → processing → complete
    ↓          ↓
  error ← ─ ─ ┘
    ↓
  retry → pending (if attempt < 3)
    ↓
  failed (if attempt ≥ 3)
```

**Job CRUD Operations:**
```python
async def create_job(chat_id: int, url: str, content_type: str, message_id: int) -> str:
    """Create new job record, return job_id"""
    job_id = str(uuid.uuid4())
    async with db.connection() as conn:
        await conn.execute("""
            INSERT INTO jobs (id, chat_id, message_id, url, content_type, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
        """, (job_id, chat_id, message_id, url, content_type))
    logger.info("job_created", extra={"job_id": job_id, "content_type": content_type})
    return job_id

async def get_job(job_id: str) -> Job:
    """Fetch job by ID"""
    async with db.connection() as conn:
        row = await conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return Job.from_row(row)

async def update_job_status(job_id: str, status: str, **kwargs) -> None:
    """Update job status and optional fields"""
    set_clause = "status = ?, updated_at = CURRENT_TIMESTAMP"
    params = [status]
    
    for key, value in kwargs.items():
        set_clause += f", {key} = ?"
        params.append(value)
    
    params.append(job_id)
    
    async with db.connection() as conn:
        await conn.execute(f"""
            UPDATE jobs SET {set_clause} WHERE id = ?
        """, params)
    
    logger.info("job_status_updated", extra={"job_id": job_id, "status": status})

async def increment_attempt(job_id: str) -> int:
    """Increment retry attempt counter, return new count"""
    async with db.connection() as conn:
        await conn.execute("""
            UPDATE jobs SET attempt = attempt + 1 WHERE id = ?
        """, (job_id,))
        row = await conn.execute("SELECT attempt FROM jobs WHERE id = ?", (job_id,))
        return row[0]
```

**Deduplication Strategy:**
```python
async def check_duplicate_job(url: str, chat_id: int, within_hours: int = 24) -> Optional[Job]:
    """Check if URL was processed recently by this user"""
    async with db.connection() as conn:
        row = await conn.execute("""
            SELECT * FROM jobs 
            WHERE url = ? 
              AND chat_id = ? 
              AND status = 'complete'
              AND created_at > datetime('now', '-{} hours')
            ORDER BY created_at DESC
            LIMIT 1
        """.format(within_hours), (url, chat_id))
        
        if row:
            return Job.from_row(row)
        return None
```

#### 2.2.4 Async Processing Layer
**Technology:** Redis for queue (or Python `asyncio.Queue` for single-instance)

**Queue Configuration:**
```python
# Using Redis
import redis.asyncio as redis

queue = redis.Redis(
    host='localhost',
    port=6379,
    db=0,
    decode_responses=True
)

async def enqueue_job(job_id: str):
    """Add job to processing queue"""
    await queue.lpush("video_jobs", job_id)
    logger.info("job_queued", extra={"job_id": job_id})

async def dequeue_job() -> Optional[str]:
    """Blocking pop from queue (30s timeout)"""
    result = await queue.brpop("video_jobs", timeout=30)
    if result:
        return result[1]  # (queue_name, job_id)
    return None
```

**Worker Process:**
```python
async def worker():
    """Background worker that processes jobs from queue"""
    logger.info("worker_started")
    
    while True:
        try:
            job_id = await dequeue_job()
            if not job_id:
                continue
            
            job = await get_job(job_id)
            logger.info("job_started", extra={
                "job_id": job_id,
                "content_type": job.content_type,
                "attempt": job.attempt
            })
            
            start_time = time.time()
            
            try:
                await update_job_status(job_id, 'processing')
                
                # Route to appropriate pipeline
                if job.content_type == 'short':
                    result = await process_short_video(job)
                else:
                    result = await process_long_video(job)
                
                # Finalize job
                await finalize_job(job, result)
                
                processing_time = int((time.time() - start_time) * 1000)
                await update_job_status(
                    job_id, 
                    'complete',
                    drive_url=result.drive_url,
                    processing_time_ms=processing_time,
                    completed_at=datetime.utcnow()
                )
                
                await send_success_message(job, result)
                
                logger.info("job_complete", extra={
                    "job_id": job_id,
                    "processing_time_ms": processing_time
                })
                
            except RetryableError as e:
                # Handle retryable errors (API timeouts, rate limits)
                await handle_retryable_error(job, e)
                
            except Exception as e:
                # Handle permanent failures
                logger.exception("job_error", extra={"job_id": job_id})
                await handle_job_error(job, e)
        
        except Exception as e:
            logger.exception("worker_error", extra={"error": str(e)})
            await asyncio.sleep(5)  # Brief pause before continuing

async def handle_retryable_error(job: Job, error: Exception):
    """Handle errors that should trigger retry"""
    attempt = await increment_attempt(job.id)
    
    if attempt < 3:
        # Exponential backoff: 5s, 15s, 45s
        delay = 5 * (3 ** (attempt - 1))
        await asyncio.sleep(delay)
        
        # Re-queue job
        await update_job_status(job.id, 'pending', error_msg=str(error))
        await enqueue_job(job.id)
        
        logger.info("job_retry_scheduled", extra={
            "job_id": job.id,
            "attempt": attempt,
            "delay_seconds": delay
        })
    else:
        # Max retries exceeded
        await update_job_status(job.id, 'error', error_msg=f"Max retries: {error}")
        await send_error_message(job, error, final=True)

async def handle_job_error(job: Job, error: Exception):
    """Handle permanent job failure"""
    await update_job_status(job.id, 'error', error_msg=str(error))
    await send_error_message(job, error, final=(job.attempt >= 3))
```

**Concurrency Control:**
```python
# Multiple workers can be spawned
async def start_workers(count: int = 3):
    """Start multiple worker processes"""
    workers = [asyncio.create_task(worker()) for _ in range(count)]
    await asyncio.gather(*workers)
```

#### 2.2.5 Short Video Pipeline
**Dependencies:**
- Local frame extraction service (`localhost:5050/short_frames`)
- Gemini 2.5 Flash Vision API
- Brave Search API (optional)

**Processing Steps:**
```python
async def process_short_video(job: Job) -> Result:
    """Process short-form video using frame analysis"""
    
    # 1. Extract frames from video
    frames = await extract_frames(job.url)
    logger.info("frames_extracted", extra={
        "job_id": job.id,
        "frame_count": len(frames)
    })
    
    # 2. Analyze frames with Gemini Vision
    analysis = await gemini_vision_analyze(frames, job.url)
    logger.info("vision_analysis_complete", extra={
        "job_id": job.id,
        "links_found": len(analysis.links),
        "text_overlays": len(analysis.text_overlays)
    })
    
    # 3. Verify links with Brave Search (optional)
    if config.ENABLE_BRAVE_SEARCH and analysis.links:
        verified_links = await verify_links(analysis.links)
        logger.info("links_verified", extra={
            "job_id": job.id,
            "verified_count": len(verified_links)
        })
    else:
        verified_links = analysis.links
    
    # 4. Build markdown summary
    markdown = build_short_video_markdown(job, analysis, verified_links)
    
    # 5. Upload to Google Drive
    drive_url = await upload_to_drive(
        content=markdown,
        filename=f"{job.id}_short_analysis.md"
    )
    logger.info("drive_upload_complete", extra={
        "job_id": job.id,
        "drive_url": drive_url
    })
    
    return Result(
        drive_url=drive_url,
        markdown=markdown,
        metadata={
            "frame_count": len(frames),
            "links_found": len(analysis.links),
            "text_overlays": len(analysis.text_overlays)
        }
    )
```

**Frame Extraction Service Contract:**
```http
POST http://localhost:5050/short_frames
Content-Type: application/json

Request:
{
  "url": "https://example.com/video.mp4",
  "max_frames": 10,
  "interval_seconds": 1
}

Response:
{
  "frames": [
    {
      "timestamp": 0.0,
      "base64": "iVBORw0KGgoAAAANSUhEUgAA..."
    },
    {
      "timestamp": 1.0,
      "base64": "iVBORw0KGgoAAAANSUhEUgAA..."
    }
  ],
  "metadata": {
    "duration_seconds": 10.5,
    "fps": 30,
    "resolution": "1920x1080"
  }
}
```

**Gemini Vision Analysis:**
```python
async def gemini_vision_analyze(frames: List[Frame], source_url: str) -> Analysis:
    """Analyze video frames using Gemini 2.5 Flash Vision"""
    
    # Build request with frames
    contents = [
        {
            "role": "user",
            "parts": [
                {"text": VISION_PROMPT},
                *[{"inline_data": {"mime_type": "image/jpeg", "data": f.base64}} for f in frames]
            ]
        }
    ]
    
    # System instruction for better token efficiency
    system_instruction = """
    You are a video content analyzer. Extract:
    1. All visible text/OCR from frames
    2. Product names, brands, logos
    3. URLs, social media handles
    4. Key visual themes
    
    Respond ONLY with valid JSON matching this schema:
    {
      "text_overlays": ["text1", "text2"],
      "brands": ["brand1", "brand2"],
      "links": ["url1", "url2"],
      "themes": ["theme1", "theme2"]
    }
    """
    
    response = await gemini_client.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
        system_instruction=system_instruction,
        generation_config={
            "temperature": 0.1,  # Low temperature for factual extraction
            "max_output_tokens": 2048
        }
    )
    
    # Parse JSON response
    try:
        result = json.loads(response.text)
        return Analysis(
            text_overlays=result.get("text_overlays", []),
            brands=result.get("brands", []),
            links=result.get("links", []),
            themes=result.get("themes", [])
        )
    except json.JSONDecodeError:
        logger.warning("gemini_invalid_json", extra={
            "response": response.text
        })
        raise RetryableError("Gemini returned invalid JSON")
```

**Brave Search Verification:**
```python
async def verify_links(links: List[str]) -> List[VerifiedLink]:
    """Verify and enrich links using Brave Search"""
    verified = []
    
    for link in links[:5]:  # Limit to top 5 links
        try:
            # Search for the domain/brand
            query = extract_domain(link)
            results = await brave_search(query, count=1)
            
            if results:
                verified.append(VerifiedLink(
                    url=link,
                    title=results[0].title,
                    description=results[0].snippet,
                    verified=True
                ))
            else:
                verified.append(VerifiedLink(
                    url=link,
                    verified=False
                ))
        except Exception as e:
            logger.warning("link_verification_failed", extra={
                "link": link,
                "error": str(e)
            })
            verified.append(VerifiedLink(url=link, verified=False))
    
    return verified
```

**Markdown Generation:**
```python
def build_short_video_markdown(job: Job, analysis: Analysis, links: List[VerifiedLink]) -> str:
    """Build markdown summary for short video analysis"""
    
    md = f"""# Short Video Analysis

**Source:** {job.url}
**Processed:** {datetime.utcnow().isoformat()}
**Job ID:** {job.id}

---

## 📊 Content Overview

### Detected Text Overlays
{chr(10).join(f"- {text}" for text in analysis.text_overlays) if analysis.text_overlays else "- None detected"}

### Brand Mentions
{chr(10).join(f"- {brand}" for brand in analysis.brands) if analysis.brands else "- None detected"}

### Visual Themes
{chr(10).join(f"- {theme}" for theme in analysis.themes) if analysis.themes else "- None detected"}

---

## 🔗 Extracted Links

"""
    
    if links:
        for link in links:
            md += f"\n### {link.url}\n"
            if link.verified:
                md += f"**{link.title}**\n\n{link.description}\n"
            else:
                md += "*Could not verify this link*\n"
    else:
        md += "No links detected in video.\n"
    
    md += f"""
---

## 📈 Processing Metadata

- **Analysis Method:** Frame-by-frame Gemini Vision
- **Frames Analyzed:** {len(analysis.text_overlays)}
- **Confidence:** High

---

*Generated by Video Intelligence Bot*
"""
    
    return md
```

#### 2.2.6 Long Video Pipeline
**Dependencies:**
- Local transcript extraction service (`localhost:5050/transcript`)
- Gemini 2.5 Flash Text API

**Processing Steps:**
```python
async def process_long_video(job: Job) -> Result:
    """Process long-form video using transcript analysis"""
    
    # 1. Extract transcript and metadata from video
    data = await extract_transcript(job.url)
    logger.info("transcript_extracted", extra={
        "job_id": job.id,
        "duration_seconds": data.duration_seconds,
        "word_count": len(data.transcript.split())
    })
    
    # 2. Enrich transcript with Gemini Text analysis
    enrichment = await gemini_text_enrich(data.transcript, data.metadata)
    logger.info("text_enrichment_complete", extra={
        "job_id": job.id,
        "summary_length": len(enrichment.summary),
        "key_points": len(enrichment.key_points)
    })
    
    # 3. Build markdown report
    markdown = build_long_video_markdown(job, data, enrichment)
    
    # 4. Upload to Google Drive
    drive_url = await upload_to_drive(
        content=markdown,
        filename=f"{job.id}_transcript_report.md"
    )
    logger.info("drive_upload_complete", extra={
        "job_id": job.id,
        "drive_url": drive_url
    })
    
    return Result(
        drive_url=drive_url,
        markdown=markdown,
        metadata={
            "duration_seconds": data.duration_seconds,
            "word_count": len(data.transcript.split()),
            "key_points": len(enrichment.key_points)
        }
    )
```

**Transcript Service Contract:**
```http
POST http://localhost:5050/transcript
Content-Type: application/json

Request:
{
  "url": "https://youtube.com/watch?v=dQw4w9WgXcQ"
}

Response:
{
  "title": "Video Title Here",
  "duration_seconds": 1234,
  "transcript": "Full transcript text with timestamps...",
  "metadata": {
    "author": "Channel Name",
    "publish_date": "2026-01-15",
    "view_count": 12345,
    "description": "Video description...",
    "tags": ["tag1", "tag2"]
  }
}
```

**Gemini Text Enrichment:**
```python
async def gemini_text_enrich(transcript: str, metadata: dict) -> Enrichment:
    """Enrich transcript with AI-generated insights"""
    
    prompt = f"""
Analyze this video transcript and provide:
1. Executive summary (3-4 sentences)
2. Key points (5-7 bullet points)
3. Main topics discussed
4. Notable quotes
5. Action items or takeaways

Video Title: {metadata['title']}
Duration: {metadata['duration_seconds']}s

Transcript:
{transcript}

Respond ONLY with valid JSON matching this schema:
{{
  "summary": "...",
  "key_points": ["point1", "point2", ...],
  "topics": ["topic1", "topic2", ...],
  "quotes": ["quote1", "quote2", ...],
  "takeaways": ["takeaway1", "takeaway2", ...]
}}
"""
    
    response = await gemini_client.generate_content(
        model="gemini-2.5-flash",
        contents=[{"role": "user", "parts": [{"text": prompt}]}],
        generation_config={
            "temperature": 0.3,
            "max_output_tokens": 4096
        }
    )
    
    try:
        result = json.loads(response.text)
        return Enrichment(
            summary=result.get("summary", ""),
            key_points=result.get("key_points", []),
            topics=result.get("topics", []),
            quotes=result.get("quotes", []),
            takeaways=result.get("takeaways", [])
        )
    except json.JSONDecodeError:
        logger.warning("gemini_invalid_json", extra={
            "response": response.text
        })
        raise RetryableError("Gemini returned invalid JSON")
```

**Markdown Generation:**
```python
def build_long_video_markdown(job: Job, data: TranscriptData, enrichment: Enrichment) -> str:
    """Build markdown report for long video analysis"""
    
    duration_str = format_duration(data.duration_seconds)
    
    md = f"""# Video Transcript Analysis

**Title:** {data.metadata['title']}
**Author:** {data.metadata['author']}
**Duration:** {duration_str}
**Published:** {data.metadata['publish_date']}

**Source:** {job.url}
**Processed:** {datetime.utcnow().isoformat()}
**Job ID:** {job.id}

---

## 📝 Executive Summary

{enrichment.summary}

---

## 🎯 Key Points

{chr(10).join(f"{i+1}. {point}" for i, point in enumerate(enrichment.key_points))}

---

## 💡 Main Topics

{chr(10).join(f"- {topic}" for topic in enrichment.topics)}

---

## 💬 Notable Quotes

{chr(10).join(f'> "{quote}"' for quote in enrichment.quotes) if enrichment.quotes else "No significant quotes extracted."}

---

## ✅ Key Takeaways

{chr(10).join(f"- {takeaway}" for takeaway in enrichment.takeaways)}

---

## 📄 Full Transcript

{data.transcript}

---

## 📊 Metadata

- **View Count:** {data.metadata.get('view_count', 'N/A'):,}
- **Word Count:** {len(data.transcript.split()):,}
- **Description:** {data.metadata.get('description', 'N/A')}
- **Tags:** {', '.join(data.metadata.get('tags', []))}

---

*Generated by Video Intelligence Bot*
"""
    
    return md

def format_duration(seconds: int) -> str:
    """Format duration in seconds to HH:MM:SS or MM:SS"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
```

#### 2.2.7 Output Layer

**Google Drive Integration:**
```python
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

async def upload_to_drive(content: str, filename: str) -> str:
    """Upload markdown file to Google Drive, return shareable link"""
    
    # Load service account credentials
    creds = service_account.Credentials.from_service_account_file(
        'service-account.json',
        scopes=['https://www.googleapis.com/auth/drive.file']
    )
    
    service = build('drive', 'v3', credentials=creds)
    
    # Prepare file metadata
    file_metadata = {
        'name': filename,
        'mimeType': 'text/markdown',
        'parents': [config.DRIVE_FOLDER_ID]
    }
    
    # Upload file
    media = MediaInMemoryUpload(
        content.encode('utf-8'),
        mimetype='text/markdown',
        resumable=True
    )
    
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id,webViewLink'
    ).execute()
    
    # Make file accessible with link
    service.permissions().create(
        fileId=file['id'],
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()
    
    logger.info("drive_upload_success", extra={
        "file_id": file['id'],
        "filename": filename
    })
    
    return file['webViewLink']
```

**Google Sheets Logging (Optional):**
```python
async def log_to_sheets(job: Job, result: Result):
    """Append job record to Google Sheets for reporting"""
    
    creds = service_account.Credentials.from_service_account_file(
        'service-account.json',
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    
    service = build('sheets', 'v4', credentials=creds)
    
    values = [[
        job.id,
        job.chat_id,
        job.url,
        job.content_type,
        job.status,
        result.drive_url,
        job.processing_time_ms,
        job.created_at.isoformat(),
        job.completed_at.isoformat()
    ]]
    
    body = {'values': values}
    
    service.spreadsheets().values().append(
        spreadsheetId=config.SHEETS_ID,
        range='Jobs!A:I',
        valueInputOption='RAW',
        body=body
    ).execute()
    
    logger.info("sheets_log_success", extra={"job_id": job.id})
```

**Telegram Response Formatting:**
```python
async def send_success_message(job: Job, result: Result):
    """Send completion message to user"""
    
    # Format metadata based on content type
    if job.content_type == 'short':
        details = f"""**Detected:**
• {result.metadata['frame_count']} frames analyzed
• {result.metadata['links_found']} links found
• {result.metadata['text_overlays']} text overlays"""
    else:
        details = f"""**Video Details:**
• Duration: {format_duration(result.metadata['duration_seconds'])}
• {result.metadata['word_count']:,} words transcribed
• {result.metadata['key_points']} key insights extracted"""
    
    message = f"""✅ **{'Short Video' if job.content_type == 'short' else 'Transcript'} Analysis Complete**

📄 [View Full Report]({result.drive_url})

{details}

⏱️ Processed in {job.processing_time_ms/1000:.1f}s
"""
    
    await telegram_send(
        chat_id=job.chat_id,
        text=message,
        parse_mode='Markdown',
        reply_to_message_id=job.message_id
    )

async def send_error_message(job: Job, error: Exception, final: bool = False):
    """Send error message with optional retry button"""
    
    if final:
        message = f"""❌ **Processing Failed (Final)**

Error: {str(error)}

The video could not be processed after {job.attempt} attempts.
Please verify the URL and try again."""
        
        keyboard = None
    else:
        message = f"""❌ **Processing Failed**

Error: {str(error)}

Attempt {job.attempt} of 3"""
        
        keyboard = {
            'inline_keyboard': [[
                {
                    'text': '🔄 Retry',
                    'callback_data': f'retry:{job.id}'
                }
            ]]
        }
    
    await telegram_send(
        chat_id=job.chat_id,
        text=message,
        reply_markup=keyboard
    )
```

**Telegram API Client:**
```python
import httpx

async def telegram_send(
    chat_id: int,
    text: str,
    parse_mode: str = 'Markdown',
    reply_markup: Optional[dict] = None,
    reply_to_message_id: Optional[int] = None
):
    """Send message via Telegram Bot API"""
    
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': parse_mode
    }
    
    if reply_markup:
        payload['reply_markup'] = reply_markup
    
    if reply_to_message_id:
        payload['reply_to_message_id'] = reply_to_message_id
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, timeout=10.0)
        response.raise_for_status()
        
    logger.info("telegram_message_sent", extra={
        "chat_id": chat_id,
        "message_preview": text[:50]
    })
```

---

## 3. User Experience

### 3.1 User Flow - Successful Processing

```
1. User sends video URL to Telegram bot
   ↓
2. Bot replies immediately (<1s): "📥 Received! Processing..."
   ↓
3. [15-90 seconds pass, user can continue using Telegram]
   ↓
4. Bot sends completion message with Drive link
   "✅ Analysis Complete
    📄 View Full Report
    ⏱️ Processed in 23.4s"
```

### 3.2 User Flow - Error with Retry

```
1. User sends video URL
   ↓
2. Processing fails (e.g., API timeout)
   ↓
3. Bot sends error message:
   "❌ Processing Failed
    Error: Gemini API timeout
    Attempt 1 of 3
    [🔄 Retry Button]"
   ↓
4. User clicks Retry button
   ↓
5. Bot edits message: "📥 Retrying..."
   ↓
6. Processing succeeds → completion message
```

### 3.3 User Flow - Duplicate Detection

```
1. User sends URL they submitted 2 hours ago
   ↓
2. Bot checks database for recent processing
   ↓
3. Bot replies immediately:
   "✅ This video was already processed recently!
    📄 [View Previous Report](drive_link)
    
    Processed 2 hours ago (23.4s)
    
    Want to re-process? Send 'force' to override."
```

### 3.4 Message Templates

**Acknowledgment:**
```
📥 Received!

Processing your video...
You'll be notified when complete.
```

**Short Video Complete:**
```
✅ **Short Video Analysis Complete**

📄 [View Full Report](drive_link)

**Detected:**
• 12 text overlays
• 3 brand logos  
• 8 product mentions

⏱️ Processed in 18.2s
```

**Long Video Complete:**
```
✅ **Transcript Analysis Complete**

📄 [View Full Report](drive_link)

**Video Details:**
• Duration: 15:32
• 2,847 words transcribed
• 7 key insights extracted

⏱️ Processed in 67.3s
```

**Error with Retry:**
```
❌ **Processing Failed**

Error: Frame extraction timeout

Attempt 2 of 3

[🔄 Retry]
```

**Permanent Failure:**
```
❌ **Processing Failed (Final)**

Error: Invalid video source

The video could not be processed after 3 attempts.
Please verify the URL and try again.
```

---

## 4. Technical Specifications

### 4.1 Technology Stack

| Layer | Technology | Version | Justification |
|-------|-----------|---------|---------------|
| Web Framework | FastAPI | 0.110+ | Async native, automatic OpenAPI docs, high performance |
| Database | SQLite | 3.40+ | Embedded, zero-config, sufficient for <10k jobs/day |
| Queue | Redis | 7.0+ | Simple pub/sub, persistent, multi-worker support |
| HTTP Client | httpx | 0.27+ | Async HTTP client for Telegram/external APIs |
| Gemini API | google-generativeai | 0.7+ | Official Google SDK |
| Google APIs | google-api-python-client | 2.120+ | Drive/Sheets integration |
| Logging | structlog | 24.1+ | Structured JSON logs for parsing |
| Deployment | Docker Compose | 2.24+ | Reproducible local environment |

### 4.2 Environment Configuration

```bash
# .env file
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_WEBHOOK_SECRET=your-random-secret-string
TELEGRAM_WEBHOOK_URL=https://yourdomain.com/webhook

GEMINI_API_KEY=AIzaSy...
GOOGLE_APPLICATION_CREDENTIALS=./service-account.json
GOOGLE_DRIVE_FOLDER_ID=1A2B3C4D5E6F7G
GOOGLE_SHEETS_ID=1X2Y3Z4...

BRAVE_SEARCH_API_KEY=BSA...
ENABLE_BRAVE_SEARCH=true

REDIS_URL=redis://localhost:6379/0

FRAME_SERVICE_URL=http://localhost:5050
TRANSCRIPT_SERVICE_URL=http://localhost:5050

LOG_LEVEL=INFO
MAX_CONCURRENT_WORKERS=3
JOB_TIMEOUT_SECONDS=120
MAX_RETRY_ATTEMPTS=3
```

### 4.3 Project Structure

```
video-intelligence-bot/
├── src/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app entry point
│   ├── config.py               # Environment config
│   ├── models.py               # Pydantic models & DB schema
│   ├── database.py             # SQLite operations
│   ├── queue.py                # Redis queue wrapper
│   ├── worker.py               # Background job processor
│   ├── telegram/
│   │   ├── __init__.py
│   │   ├── webhook.py          # Webhook handler
│   │   ├── sender.py           # Message sender
│   │   └── formatter.py        # Message templates
│   ├── processors/
│   │   ├── __init__.py
│   │   ├── short_video.py      # Short video pipeline
│   │   ├── long_video.py       # Long video pipeline
│   │   ├── gemini.py           # Gemini API client
│   │   └── brave.py            # Brave Search client
│   ├── services/
│   │   ├── __init__.py
│   │   ├── frames.py           # Frame extraction client
│   │   ├── transcript.py       # Transcript extraction client
│   │   ├── drive.py            # Google Drive uploader
│   │   └── sheets.py           # Google Sheets logger
│   └── utils/
│       ├── __init__.py
│       ├── logger.py           # Structured logging
│       ├── validators.py       # URL validation
│       └── markdown.py         # Markdown builders
├── tests/
│   ├── test_api.py
│   ├── test_worker.py
│   ├── test_processors.py
│   ├── test_database.py
│   └── fixtures/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DECISIONS.md
│   ├── SCALING.md
│   └── WHY.md
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
└── service-account.json         # Google API credentials (not in git)
```

### 4.4 Docker Compose Configuration

```yaml
version: '3.8'

services:
  api:
    build: .
    container_name: video-bot-api
    env_file: .env
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
      - ./service-account.json:/app/service-account.json:ro
    depends_on:
      - redis
    restart: unless-stopped
    command: uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
    networks:
      - video-bot-network

  worker:
    build: .
    container_name: video-bot-worker
    env_file: .env
    volumes:
      - ./data:/app/data
      - ./logs:/app/logs
      - ./service-account.json:/app/service-account.json:ro
    depends_on:
      - redis
    restart: unless-stopped
    command: python -m src.worker
    networks:
      - video-bot-network

  redis:
    image: redis:7-alpine
    container_name: video-bot-redis
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    restart: unless-stopped
    command: redis-server --appendonly yes
    networks:
      - video-bot-network

volumes:
  redis_data:

networks:
  video-bot-network:
    driver: bridge
```

### 4.5 Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/

# Create data and log directories
RUN mkdir -p /app/data /app/logs

# Expose API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:8000/health')"

# Default command (overridden in docker-compose)
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 4.6 Requirements.txt

```txt
fastapi==0.110.0
uvicorn[standard]==0.27.0
httpx==0.27.0
redis==5.0.1
google-generativeai==0.7.0
google-api-python-client==2.120.0
google-auth==2.28.0
structlog==24.1.0
pydantic==2.6.1
pydantic-settings==2.1.0
python-dotenv==1.0.1
aiosqlite==0.19.0
```

### 4.7 Logging Schema

```json
{
  "timestamp": "2026-05-11T12:34:56.789Z",
  "level": "info",
  "event": "job_started",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "chat_id": 987654321,
  "content_type": "short",
  "url": "https://example.com/video.mp4",
  "attempt": 1
}
```

**Key Events to Log:**
- `job_created` - User submitted URL
- `job_queued` - Added to processing queue
- `job_started` - Worker picked up job
- `frames_extracted` - Frame extraction complete (with count)
- `vision_analysis_complete` - Gemini Vision returned
- `transcript_extracted` - Transcript fetched (with word count)
- `text_enrichment_complete` - Gemini Text enrichment done
- `drive_upload_complete` - Markdown uploaded to Drive
- `job_complete` - Full pipeline finished (with processing time)
- `job_error` - Failure occurred (with error type)
- `retry_triggered` - Job re-queued for retry

**Structured Logging Setup:**
```python
import structlog

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=False
)

logger = structlog.get_logger()
```

**Log Queries:**
```bash
# Find all failed jobs in the last hour
cat logs/app.log | jq 'select(.event=="job_error" and .timestamp > "2026-05-11T11:00:00")'

# Calculate average processing time by content type
cat logs/app.log | jq 'select(.event=="job_complete") | {content_type, processing_time_ms}' | jq -s 'group_by(.content_type) | map({content_type: .[0].content_type, avg_ms: (map(.processing_time_ms) | add / length)})'

# Count jobs processed per hour
cat logs/app.log | jq 'select(.event=="job_complete")' | jq -r '.timestamp[0:13]' | sort | uniq -c
```

### 4.8 Performance Requirements

| Metric | Target | Measurement |
|--------|--------|-------------|
| Webhook response time | < 200ms | p95 latency |
| Short video processing | < 30s | p50 latency |
| Long video processing | < 90s | p50 latency |
| Job failure rate | < 2% | Errors / total jobs |
| Queue lag | < 5 jobs | Current queue depth |
| Database query time | < 50ms | p95 latency |
| Worker uptime | > 99% | Availability |

### 4.9 Error Handling Strategy

**Error Classification:**
```python
class ErrorType(Enum):
    USER_ERROR = "user_error"              # Invalid input, no retry
    RETRYABLE_ERROR = "retryable_error"    # Temporary, should retry
    SYSTEM_ERROR = "system_error"          # Critical, needs intervention

# Examples
USER_ERRORS = {
    'invalid_url': 'URL format is invalid',
    'unsupported_format': 'Video format not supported',
    'url_not_accessible': 'Cannot access URL (404/403)'
}

RETRYABLE_ERRORS = {
    'gemini_timeout': 'Gemini API request timeout',
    'gemini_rate_limit': 'Gemini API rate limit exceeded',
    'frame_extraction_timeout': 'Frame service timeout',
    'transcript_timeout': 'Transcript service timeout',
    'drive_upload_failed': 'Google Drive temporary error'
}

SYSTEM_ERRORS = {
    'database_error': 'SQLite database failure',
    'redis_connection': 'Redis connection lost',
    'auth_failed': 'Google API authentication failed',
    'disk_full': 'Insufficient disk space'
}
```

**Retry Policy:**
```python
RETRY_CONFIG = {
    'max_attempts': 3,
    'backoff_multiplier': 3,        # 5s, 15s, 45s
    'base_delay_seconds': 5,
    'retryable_error_types': [
        ErrorType.RETRYABLE_ERROR
    ]
}

def calculate_backoff_delay(attempt: int) -> int:
    """Calculate exponential backoff delay"""
    return RETRY_CONFIG['base_delay_seconds'] * (RETRY_CONFIG['backoff_multiplier'] ** (attempt - 1))
```

---

## 5. Deployment & Operations

### 5.1 Local Development Setup

```bash
# 1. Clone repository
git clone https://github.com/yourusername/video-intelligence-bot.git
cd video-intelligence-bot

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your credentials:
#   - TELEGRAM_BOT_TOKEN
#   - GEMINI_API_KEY
#   - Google service account JSON
#   - etc.

# 5. Initialize database
python -m src.database init

# 6. Start services
docker-compose up -d redis  # Start Redis only

# 7. Run API server (development mode with auto-reload)
uvicorn src.main:app --reload --port 8000

# 8. In another terminal, start worker
python -m src.worker

# 9. Set up Telegram webhook (ngrok for local testing)
ngrok http 8000
# Copy ngrok HTTPS URL
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
     -d "url=https://your-ngrok-url.ngrok.io/webhook"

# 10. Test the bot
# Send a video URL to your Telegram bot
```

### 5.2 Production Deployment (VPS)

```bash
# On VPS (Ubuntu 24.04)
ssh user@your-server-ip

# Install dependencies
sudo apt update
sudo apt install docker.io docker-compose git -y
sudo systemctl enable docker
sudo systemctl start docker

# Clone and configure
git clone https://github.com/yourusername/video-intelligence-bot.git
cd video-intelligence-bot
cp .env.example .env
nano .env  # Configure production values

# Add Google service account credentials
nano service-account.json  # Paste JSON content

# Start all services
docker-compose up -d

# Verify health
curl http://localhost:8000/health

# Set Telegram webhook to VPS IP/domain
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
     -d "url=https://your-domain.com/webhook"

# Monitor logs
docker-compose logs -f --tail=100
```

### 5.3 Health Monitoring

**Health Check Endpoint:**
```python
@app.get("/health")
async def health_check():
    """Comprehensive health check"""
    
    # Check database
    try:
        async with db.connection() as conn:
            await conn.execute("SELECT 1")
        db_status = "healthy"
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"
    
    # Check Redis
    try:
        await redis.ping()
        queue_depth = await redis.llen("video_jobs")
        redis_status = "healthy"
    except Exception as e:
        redis_status = f"unhealthy: {str(e)}"
        queue_depth = None
    
    # Check worker status (via Redis heartbeat)
    worker_last_heartbeat = await redis.get("worker:heartbeat")
    worker_healthy = (
        worker_last_heartbeat and 
        (time.time() - float(worker_last_heartbeat)) < 60
    )
    
    overall_status = (
        "healthy" if all([
            db_status == "healthy",
            redis_status == "healthy",
            worker_healthy
        ]) else "degraded"
    )
    
    return {
        "status": overall_status,
        "timestamp": datetime.utcnow().isoformat(),
        "uptime_seconds": time.time() - start_time,
        "components": {
            "database": db_status,
            "redis": redis_status,
            "worker": "healthy" if worker_healthy else "unhealthy"
        },
        "queue_depth": queue_depth
    }
```

**Metrics to Monitor:**
```python
# Add Prometheus metrics (optional)
from prometheus_client import Counter, Histogram, Gauge

jobs_processed = Counter('jobs_processed_total', 'Total jobs processed', ['content_type', 'status'])
processing_time = Histogram('job_processing_seconds', 'Job processing time', ['content_type'])
queue_depth = Gauge('queue_depth', 'Current queue depth')
worker_count = Gauge('active_workers', 'Number of active workers')
```

### 5.4 Backup & Recovery

**Database Backup:**
```bash
# Automated daily backup (add to crontab)
0 3 * * * docker exec video-bot-api sqlite3 /app/data/jobs.db ".backup '/app/data/backups/jobs_$(date +\%Y\%m\%d).db'"

# Keep last 7 days
0 4 * * * find /app/data/backups -name "jobs_*.db" -mtime +7 -delete
```

**Redis Persistence:**
```yaml
# In docker-compose.yml
redis:
  command: redis-server --appendonly yes --appendfsync everysec
  volumes:
    - redis_data:/data  # Persisted to disk
```

**Recovery Procedure:**
```bash
# Restore database from backup
docker-compose down
cp data/backups/jobs_20260511.db data/jobs.db
docker-compose up -d

# Verify data integrity
docker exec video-bot-api sqlite3 /app/data/jobs.db "PRAGMA integrity_check;"
```

---

## 6. Testing Strategy

### 6.1 Unit Tests

```python
# tests/test_validators.py
import pytest
from src.utils.validators import is_valid_video_url, detect_content_type

def test_valid_youtube_url():
    assert is_valid_video_url("https://youtube.com/watch?v=abc123")

def test_invalid_url():
    assert not is_valid_video_url("not a url")

def test_detect_short_video():
    assert detect_content_type("https://example.com/short.mp4") == "short"

def test_detect_long_video():
    assert detect_content_type("https://youtube.com/watch?v=abc") == "long"

# tests/test_database.py
import pytest
from src.database import create_job, get_job, update_job_status

@pytest.mark.asyncio
async def test_create_and_retrieve_job():
    job_id = await create_job(
        chat_id=12345,
        url="https://test.com/video.mp4",
        content_type="short",
        message_id=67890
    )
    
    job = await get_job(job_id)
    assert job.chat_id == 12345
    assert job.status == "pending"

@pytest.mark.asyncio
async def test_update_job_status():
    job_id = await create_job(12345, "https://test.com/video.mp4", "short", 67890)
    await update_job_status(job_id, "complete", drive_url="https://drive.google.com/...")
    
    job = await get_job(job_id)
    assert job.status == "complete"
    assert job.drive_url is not None
```

### 6.2 Integration Tests

```python
# tests/test_api.py
import pytest
from httpx import AsyncClient
from src.main import app

@pytest.mark.asyncio
async def test_webhook_creates_job():
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post("/webhook", json={
            "message": {
                "message_id": 123,
                "chat": {"id": 456},
                "text": "https://youtube.com/watch?v=test"
            }
        }, headers={
            "X-Telegram-Bot-Api-Secret-Token": "test-secret"
        })
        
        assert response.status_code == 200
        
        # Verify job created
        # (Query database to confirm)

@pytest.mark.asyncio
async def test_health_endpoint():
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
```

### 6.3 End-to-End Tests

```python
# tests/test_e2e.py
import pytest
from tests.fixtures import mock_telegram_bot, mock_gemini_api

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_short_video_full_pipeline(mock_telegram_bot, mock_gemini_api):
    """Test complete short video processing flow"""
    
    # 1. Submit URL via webhook
    job_id = await submit_video_url("https://test.com/short.mp4", chat_id=12345)
    
    # 2. Wait for processing
    await asyncio.sleep(5)
    
    # 3. Verify job completed
    job = await get_job(job_id)
    assert job.status == "complete"
    assert job.drive_url is not None
    
    # 4. Verify Telegram message sent
    assert mock_telegram_bot.messages_sent == 2  # Acknowledgment + completion

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_retry_on_failure():
    """Test retry mechanism on transient failures"""
    
    # Configure mock to fail twice, succeed on third attempt
    mock_gemini.set_failure_count(2)
    
    job_id = await submit_video_url("https://test.com/video.mp4", chat_id=12345)
    
    # Wait for retries
    await asyncio.sleep(70)  # 5s + 15s + 45s + processing
    
    job = await get_job(job_id)
    assert job.status == "complete"
    assert job.attempt == 3
```

### 6.4 Load Testing

```python
# tests/load_test.py
import asyncio
import time

async def load_test(concurrent_users: int = 50, duration_seconds: int = 60):
    """Simulate concurrent users submitting videos"""
    
    start_time = time.time()
    jobs_submitted = 0
    jobs_completed = 0
    jobs_failed = 0
    
    async def submit_job(user_id: int):
        nonlocal jobs_submitted, jobs_completed, jobs_failed
        
        while time.time() - start_time < duration_seconds:
            try:
                job_id = await submit_video_url(
                    f"https://test.com/video_{user_id}_{jobs_submitted}.mp4",
                    chat_id=user_id
                )
                jobs_submitted += 1
                
                # Wait for completion (with timeout)
                job = await wait_for_job_completion(job_id, timeout=120)
                
                if job.status == "complete":
                    jobs_completed += 1
                else:
                    jobs_failed += 1
                    
            except Exception as e:
                jobs_failed += 1
                logger.error(f"Load test error: {e}")
            
            await asyncio.sleep(1)  # 1 req/sec per user
    
    # Spawn concurrent users
    tasks = [submit_job(i) for i in range(concurrent_users)]
    await asyncio.gather(*tasks)
    
    # Report results
    print(f"""
Load Test Results:
- Duration: {duration_seconds}s
- Concurrent Users: {concurrent_users}
- Jobs Submitted: {jobs_submitted}
- Jobs Completed: {jobs_completed}
- Jobs Failed: {jobs_failed}
- Success Rate: {jobs_completed/jobs_submitted*100:.1f}%
- Throughput: {jobs_completed/duration_seconds:.1f} jobs/sec
""")

if __name__ == "__main__":
    asyncio.run(load_test(concurrent_users=50, duration_seconds=60))
```

**Expected Results:**
```
Load Test Results:
- Duration: 60s
- Concurrent Users: 50
- Jobs Submitted: 3000
- Jobs Completed: 2940
- Jobs Failed: 60
- Success Rate: 98.0%
- Throughput: 49.0 jobs/sec

Queue Depth (peak): 12 jobs
Average Processing Time: 28.3s (short), 72.1s (long)
```

---

## 7. Security Considerations

### 7.1 Authentication & Authorization

**Telegram Webhook Validation:**
```python
def validate_telegram_webhook(request: Request) -> bool:
    """Validate incoming webhook using secret token"""
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    return secrets.compare_digest(secret or "", config.TELEGRAM_WEBHOOK_SECRET)

@app.post("/webhook")
async def webhook(request: Request):
    if not validate_telegram_webhook(request):
        logger.warning("webhook_unauthorized_attempt", extra={
            "ip": request.client.host
        })
        raise HTTPException(status_code=403, detail="Unauthorized")
    # ... process webhook
```

**API Key for Internal Endpoints:**
```python
def verify_api_key(api_key: str = Header(..., alias="X-API-Key")):
    """Verify API key for internal endpoints"""
    if not secrets.compare_digest(api_key, config.INTERNAL_API_KEY):
        raise HTTPException(status_code=403, detail="Invalid API key")

@app.get("/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def get_job_status(job_id: str):
    job = await get_job(job_id)
    return job.dict()
```

### 7.2 Input Validation

**URL Validation & SSRF Prevention:**
```python
import re
from urllib.parse import urlparse

BLOCKED_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0', '169.254.169.254']
ALLOWED_SCHEMES = ['http', 'https']
URL_PATTERN = re.compile(
    r'^https?://'  # http:// or https://
    r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain...
    r'localhost|'  # localhost...
    r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or IP
    r'(?::\d+)?'  # optional port
    r'(?:/?|[/?]\S+)$', re.IGNORECASE
)

def is_valid_video_url(url: str) -> bool:
    """Validate URL and prevent SSRF attacks"""
    
    # Basic format check
    if not URL_PATTERN.match(url):
        return False
    
    parsed = urlparse(url)
    
    # Check scheme
    if parsed.scheme not in ALLOWED_SCHEMES:
        return False
    
    # Prevent SSRF - block internal IPs
    if parsed.hostname in BLOCKED_HOSTS:
        logger.warning("ssrf_attempt_blocked", extra={"url": url})
        return False
    
    # Block private IP ranges
    if parsed.hostname:
        try:
            import ipaddress
            ip = ipaddress.ip_address(parsed.hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                logger.warning("private_ip_blocked", extra={"url": url})
                return False
        except ValueError:
            pass  # Not an IP, hostname is fine
    
    return True
```

**Content Type Detection:**
```python
def detect_content_type(url: str) -> str:
    """Detect if URL is short or long video"""
    
    # YouTube, Vimeo, etc = long video
    long_video_domains = ['youtube.com', 'youtu.be', 'vimeo.com']
    parsed = urlparse(url)
    
    if any(domain in parsed.hostname for domain in long_video_domains):
        return 'long'
    
    # Direct video files = short video
    short_video_extensions = ['.mp4', '.mov', '.avi', '.webm', '.mkv']
    if any(url.lower().endswith(ext) for ext in short_video_extensions):
        return 'short'
    
    # Default to short
    return 'short'
```

### 7.3 Rate Limiting

**Per-User Rate Limiting:**
```python
from datetime import datetime, timedelta

class RateLimiter:
    """Simple in-memory rate limiter"""
    
    def __init__(self, requests_per_hour: int = 10):
        self.requests_per_hour = requests_per_hour
        self.requests = {}  # {chat_id: [timestamp, ...]}
    
    async def is_allowed(self, chat_id: int) -> bool:
        """Check if user is within rate limit"""
        now = datetime.utcnow()
        hour_ago = now - timedelta(hours=1)
        
        # Get user's recent requests
        if chat_id not in self.requests:
            self.requests[chat_id] = []
        
        # Remove requests older than 1 hour
        self.requests[chat_id] = [
            ts for ts in self.requests[chat_id] if ts > hour_ago
        ]
        
        # Check limit
        if len(self.requests[chat_id]) >= self.requests_per_hour:
            return False
        
        # Add current request
        self.requests[chat_id].append(now)
        return True

rate_limiter = RateLimiter(requests_per_hour=10)

@app.post("/webhook")
async def webhook(request: Request):
    # ... validation ...
    
    chat_id = message.get("chat", {}).get("id")
    
    if not await rate_limiter.is_allowed(chat_id):
        await send_message(
            chat_id=chat_id,
            text="⚠️ Rate limit exceeded. You can submit 10 videos per hour.\nPlease try again later."
        )
        return {"ok": True}
    
    # ... process request ...
```

### 7.4 Data Privacy

**Minimal Data Storage:**
```python
# Only store essential data, no personal info
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,        -- Telegram chat ID only
    message_id INTEGER,               -- For reply threading
    url TEXT NOT NULL,                -- Source URL (may contain tokens)
    # NO: username, first_name, last_name, phone_number, etc.
)
```

**URL Sanitization:**
```python
def sanitize_url_for_logging(url: str) -> str:
    """Remove sensitive parameters from URL before logging"""
    parsed = urlparse(url)
    
    # Remove query parameters that might contain tokens
    sensitive_params = ['token', 'key', 'auth', 'session', 'api_key']
    
    if parsed.query:
        from urllib.parse import parse_qs, urlencode
        params = parse_qs(parsed.query)
        
        # Remove sensitive params
        filtered = {k: v for k, v in params.items() if k.lower() not in sensitive_params}
        
        # Reconstruct URL
        clean_query = urlencode(filtered, doseq=True)
        return parsed._replace(query=clean_query).geturl()
    
    return url

# Use in logging
logger.info("job_created", extra={
    "job_id": job_id,
    "url": sanitize_url_for_logging(url)  # Safe for logs
})
```

**Google Drive Permissions:**
```python
async def upload_to_drive(content: str, filename: str) -> str:
    # ... upload file ...
    
    # Set permission to "anyone with link" (not public indexed)
    service.permissions().create(
        fileId=file['id'],
        body={
            'type': 'anyone',
            'role': 'reader',
            'withLink': True  # Not discoverable without link
        }
    ).execute()
    
    return file['webViewLink']
```

### 7.5 Secrets Management

**Never Commit Secrets:**
```bash
# .gitignore
.env
service-account.json
*.key
*.pem
secrets/
```

**Environment Variables Only:**
```python
# config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_webhook_secret: str
    gemini_api_key: str
    google_application_credentials: str
    
    class Config:
        env_file = ".env"
        case_sensitive = False

config = Settings()

# NEVER do this:
# TELEGRAM_BOT_TOKEN = "1234567890:ABCdef..."  # ❌ HARDCODED
```

---

## 8. Migration from n8n

### 8.1 Migration Strategy

**Phase 1: Parallel Run (Week 1-2)**
- Deploy Python service with different Telegram bot token (test bot)
- Process same URLs through both n8n and Python
- Compare outputs for accuracy and performance
- Fix any discrepancies

**Phase 2: Gradual Cutover (Week 3)**
- Route 10% of traffic to Python service (random sampling)
- Monitor error rates, processing time, user complaints
- If stable for 48h, increase to 50%
- If stable for 48h, increase to 100%

**Phase 3: Data Migration (Week 4)**
- Export historical job data from Google Sheets
- Import into SQLite database
- Verify data integrity
- Archive n8n workflow JSON

**Phase 4: Decommission (Week 5)**
- Stop n8n workflow completely
- Remove n8n Docker containers
- Update all documentation
- Monitor Python service for 1 week

### 8.2 Rollback Plan

If Python service fails in production:

1. **Immediate:** Redirect Telegram webhook back to n8n
   ```bash
   curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
        -d "url=https://n8n-instance.com/webhook/video-bot"
   ```

2. **Investigate:** Check Python service logs
   ```bash
   docker-compose logs -f --tail=500 api worker
   ```

3. **Fix:** Address issues in development environment
4. **Retry:** Repeat migration phases after fixes

### 8.3 Data Export from Google Sheets

```python
# scripts/export_sheets_data.py
import asyncio
from google.oauth2 import service_account
from googleapiclient.discovery import build
import sqlite3

async def export_sheets_to_sqlite():
    # Connect to Sheets
    creds = service_account.Credentials.from_service_account_file(
        'service-account.json',
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
    )
    service = build('sheets', 'v4', credentials=creds)
    
    # Read all rows
    result = service.spreadsheets().values().get(
        spreadsheetId=config.SHEETS_ID,
        range='Jobs!A2:I'  # Assuming headers in row 1
    ).execute()
    
    rows = result.get('values', [])
    
    # Insert into SQLite
    conn = sqlite3.connect('data/jobs.db')
    cursor = conn.cursor()
    
    for row in rows:
        cursor.execute("""
            INSERT OR IGNORE INTO jobs 
            (id, chat_id, url, content_type, status, drive_url, processing_time_ms, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, row)
    
    conn.commit()
    conn.close()
    
    print(f"Exported {len(rows)} jobs from Sheets to SQLite")

if __name__ == "__main__":
    asyncio.run(export_sheets_to_sqlite())
```

---

## 9. Future Enhancements

### 9.1 Short-Term (Next 3 Months)

**1. Duplicate Detection Enhancement**
```python
# Add URL hash column for faster lookups
CREATE INDEX idx_url_hash ON jobs(url);

# Check if URL was processed in last 24h
SELECT * FROM jobs 
WHERE url = ? 
  AND status = 'complete' 
  AND created_at > datetime('now', '-24 hours')
ORDER BY created_at DESC 
LIMIT 1;
```

**2. User Preferences**
```python
# New table for user settings
CREATE TABLE user_preferences (
    chat_id INTEGER PRIMARY KEY,
    enable_brave_search BOOLEAN DEFAULT TRUE,
    preferred_language TEXT DEFAULT 'en',
    notification_level TEXT DEFAULT 'all',  # all, errors_only, none
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**3. Job History Command**
```python
# Telegram command: /history
@app.message_handler(commands=['history'])
async def show_history(message):
    chat_id = message.chat.id
    
    jobs = await db.execute("""
        SELECT url, status, created_at 
        FROM jobs 
        WHERE chat_id = ? 
        ORDER BY created_at DESC 
        LIMIT 10
    """, (chat_id,))
    
    text = "📋 Your Recent Jobs:\n\n"
    for job in jobs:
        status_emoji = "✅" if job['status'] == 'complete' else "❌"
        text += f"{status_emoji} {job['url'][:50]}...\n"
        text += f"   {job['created_at']}\n\n"
    
    await send_message(chat_id, text)
```

### 9.2 Long-Term (6-12 Months)

**1. Batch Processing**
```python
# Accept playlist URLs
# Telegram command: /batch
# User sends: "https://youtube.com/playlist?list=..."
# Bot processes all videos in playlist
```

**2. Web Dashboard**
```python
# Simple FastAPI + HTML dashboard
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    stats = await get_stats()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "stats": stats
    })
```

**3. Multi-Language Support**
```python
# Detect video language, translate transcript
from deep_translator import GoogleTranslator

async def translate_if_needed(text: str, target_lang: str = 'en') -> str:
    # Detect language
    detected = detect_language(text)
    
    if detected != target_lang:
        translated = GoogleTranslator(source=detected, target=target_lang).translate(text)
        return translated
    
    return text
```

**4. Real-Time Streaming**
```python
# Process live YouTube streams
# Send periodic updates (every 5 minutes)
# Full analysis when stream ends
```

---

## 10. Success Criteria & KPIs

### 10.1 Technical Metrics

| Metric | Current (n8n) | Target (Python) | Measurement |
|--------|---------------|-----------------|-------------|
| Codebase size | 60+ nodes | < 600 LOC Python | Lines of code |
| Avg processing time (short) | ~35s | < 30s | p50 latency |
| Avg processing time (long) | ~95s | < 90s | p50 latency |
| Job failure rate | ~5% | < 2% | Failed / total jobs |
| Time to add feature | ~2-3 days | < 4 hours | Developer survey |
| Deployment time | ~20 min | < 5 min | `docker-compose up` |

### 10.2 Operational Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Uptime | > 99% | Weekly availability |
| MTTR (Mean Time To Recovery) | < 10 minutes | Incident logs |
| Database query time | < 50ms (p95) | Structured logs |
| Queue lag | < 5 jobs | Real-time monitoring |
| Worker crashes | 0 per week | Health checks |

### 10.3 User Experience Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Acknowledgment latency | < 1s | Webhook response time |
| Error message clarity | User survey > 8/10 | Post-error feedback |
| Retry success rate | > 80% | Retry → complete % |
| Drive link availability | 100% | Link click success |

---

## 11. Risks & Mitigations

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| Gemini API rate limits | High | Medium | Queue management, backoff, alert before limit |
| Frame extraction service crashes | High | Low | Health checks, auto-restart, timeout handling |
| SQLite database corruption | High | Very Low | Daily backups, WAL mode, integrity checks |
| Telegram API changes | Medium | Low | Version pinning, monitor changelog |
| Google Drive quota exceeded | Medium | Medium | Monitor usage, implement cleanup policy |
| Worker process hangs | Medium | Low | Job timeout, watchdog, auto-restart |
| Redis connection loss | Medium | Low | Connection pooling, retry logic, fallback to asyncio.Queue |
| Local machine downtime | High | High (laptop) | Document VPS migration path, accept risk for portfolio |

---

## 12. Open Questions

1. **Should we support additional video platforms?** (TikTok, Instagram, Twitter/X)
   - Decision: Start with YouTube + direct MP4 URLs only, add others based on user requests

2. **How long should we keep job records?**
   - Proposal: 90 days for completed jobs, 30 days for failed jobs
   - Implement auto-cleanup cron job

3. **Should we add a web dashboard for job monitoring?**
   - Decision: Not in MVP, but prepare architecture to support it (API endpoints ready)

4. **What's the maximum video duration we support?**
   - Proposal: 2 hours for long videos (transcript), 5 minutes for short videos (frames)
   - Reject longer videos with clear error message

5. **Should we implement user authentication beyond Telegram?**
   - Decision: No, Telegram chat_id is sufficient authentication for now

---

## 13. Appendices

### Appendix A: Glossary

- **Job:** A single video processing request from a user
- **Content Type:** Classification of video as "short" (< 5min, frame-based) or "long" (transcript-based)
- **Worker:** Background process that pulls jobs from queue and executes processing pipeline
- **State Machine:** The status lifecycle of a job (pending → processing → complete/error)
- **Retry:** Automatic re-attempt of failed job with exponential backoff
- **SSRF:** Server-Side Request Forgery attack (prevented via URL validation)

### Appendix B: References

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Gemini API Documentation](https://ai.google.dev/docs)
- [Google Drive API v3](https://developers.google.com/drive/api/v3/reference)
- [Structlog Documentation](https://www.structlog.org/)
- [Redis Documentation](https://redis.io/docs/)
- [SQLite Documentation](https://www.sqlite.org/docs.html)

### Appendix C: Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-05-11 | Use SQLite over PostgreSQL initially | Simpler deployment, sufficient for current scale (<10k jobs/day) |
| 2026-05-11 | Keep Google Sheets for reporting only | Maintains historical data continuity, low migration risk |
| 2026-05-11 | Use direct Telegram API over wrapper library | Full control, no wrapper library version conflicts |
| 2026-05-11 | Implement retry via Telegram callbacks | Better UX than auto-retry, user confirms intent |
| 2026-05-11 | Run on local machine (not VPS) | Portfolio project, accept downtime risk for simplicity |
| 2026-05-11 | Use Redis for queue (not asyncio.Queue) | Enables multi-worker scaling if needed later |

### Appendix D: Comparison with n8n Workflow

| Aspect | n8n Workflow | Python Service | Winner |
|--------|-------------|----------------|--------|
| **Maintainability** | 60+ nodes, visual spaghetti | ~500 LOC Python, clear structure | Python |
| **Performance** | ~35s short, ~95s long | Target <30s, <90s | Python |
| **Observability** | Limited logging, no metrics | Structured logs, queryable | Python |
| **Debugging** | Click through nodes | Standard Python debugger | Python |
| **Version Control** | Giant JSON blob | Standard git workflow | Python |
| **Testing** | Manual only | Unit + integration + e2e | Python |
| **Deployment** | Docker + n8n container | Docker only | Python |
| **Learning Curve** | n8n-specific knowledge | Standard Python/FastAPI | Python |
| **Flexibility** | Limited to n8n nodes | Unlimited with Python | Python |
| **Cost** | Free (self-hosted) | Free (self-hosted) | Tie |

**Verdict:** Python service wins on all technical dimensions. n8n has no advantages for this use case.

---

**Document Status:** ✅ Ready for Implementation

**Next Steps:**
1. Technical review (1 week)
2. Set up development environment
3. Implement MVP (2-3 weeks)
4. Testing & QA (1 week)
5. Parallel run with n8n (2 weeks)
6. Full migration (1 week)

**Estimated Total Timeline:** 7-9 weeks from approval to full production deployment
