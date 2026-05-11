# Video Intelligence Bot - Standalone Python Architecture

## System Architecture Diagram

This diagram shows the complete architecture of the Video Intelligence Bot built as a standalone Python service, replacing the n8n workflow implementation.

```mermaid
graph TB
    subgraph "Telegram Bot Layer"
        TG[Telegram Webhook<br/>FastAPI endpoint]
        TG_SEND[Telegram Send Message]
        TG_CALLBACK[Telegram Callback Handler<br/>for retry buttons]
    end

    subgraph "API Layer - FastAPI"
        API[/webhook endpoint]
        VALIDATE[URL Validator<br/>detect content type]
        ROUTER[Content Type Router<br/>short vs long]
        CALLBACK_API[/callback endpoint<br/>for user retry actions]
        HEALTH[/health endpoint]
    end

    subgraph "Job Management"
        DB[(SQLite<br/>jobs table)]
        JOB_CREATE[Create Job Record<br/>status: pending]
        JOB_UPDATE[Update Job Status<br/>complete/error]
        JOB_GET[Get Job by ID]
        JOB_DEDUP[Check Duplicate URL]
    end

    subgraph "Processing Services - Async Workers"
        QUEUE[Redis Queue or<br/>asyncio.Queue]
        WORKER[Background Worker<br/>processes pending jobs]
    end

    subgraph "Short Video Pipeline"
        FRAME_SVC[Frame Extraction Service<br/>localhost:5050/short_frames]
        GEMINI_VISION[Gemini 2.5 Flash<br/>Vision API]
        BRAVE[Brave Search API<br/>optional link verification]
        MD_SHORT[Build Markdown Summary]
    end

    subgraph "Long Video Pipeline"
        TRANSCRIPT_SVC[Transcript Service<br/>localhost:5050/transcript]
        GEMINI_TEXT[Gemini 2.5 Flash<br/>Text Enrichment]
        MD_LONG[Build Markdown Report]
    end

    subgraph "Output Layer"
        DRIVE[Google Drive Upload<br/>store markdown]
        SHEETS[Google Sheets Logger<br/>append row for reporting]
        FORMAT[Format Telegram Response<br/>with file link]
    end

    %% Telegram incoming flow
    TG -->|POST /webhook| API
    API --> VALIDATE
    VALIDATE -->|valid| JOB_DEDUP
    VALIDATE -->|invalid| TG_SEND
    
    JOB_DEDUP -->|new URL| ROUTER
    JOB_DEDUP -->|duplicate| TG_SEND
    
    ROUTER --> JOB_CREATE
    JOB_CREATE --> DB
    JOB_CREATE --> QUEUE
    JOB_CREATE -->|immediate ack| TG_SEND

    %% Health check
    HEALTH -.-> DB
    HEALTH -.-> QUEUE
    HEALTH -.-> WORKER

    %% Worker processing
    QUEUE --> WORKER
    WORKER --> JOB_GET
    JOB_GET --> DB
    
    WORKER -->|short video| FRAME_SVC
    WORKER -->|long video| TRANSCRIPT_SVC

    %% Short video path
    FRAME_SVC --> GEMINI_VISION
    GEMINI_VISION --> BRAVE
    BRAVE --> MD_SHORT
    MD_SHORT --> DRIVE

    %% Long video path
    TRANSCRIPT_SVC --> GEMINI_TEXT
    GEMINI_TEXT --> MD_LONG
    MD_LONG --> DRIVE

    %% Completion
    DRIVE --> SHEETS
    SHEETS --> FORMAT
    FORMAT --> JOB_UPDATE
    JOB_UPDATE --> DB
    FORMAT --> TG_SEND

    %% Error handling
    WORKER -.->|on error| JOB_UPDATE
    JOB_UPDATE -.->|status: error| TG_CALLBACK
    TG_CALLBACK -->|user clicks retry| CALLBACK_API
    CALLBACK_API --> JOB_GET
    CALLBACK_API -->|reset to pending| JOB_UPDATE
    JOB_UPDATE -.->|back to queue| QUEUE

    %% Styling
    classDef telegram fill:#0088cc,stroke:#005580,color:#fff
    classDef api fill:#ff6b6b,stroke:#cc0000,color:#fff
    classDef processing fill:#4ecdc4,stroke:#2a9d8f,color:#000
    classDef storage fill:#ffe66d,stroke:#f4a261,color:#000
    classDef external fill:#a8dadc,stroke:#457b9d,color:#000

    class TG,TG_SEND,TG_CALLBACK telegram
    class API,VALIDATE,ROUTER,CALLBACK_API,HEALTH api
    class WORKER,FRAME_SVC,TRANSCRIPT_SVC,GEMINI_VISION,GEMINI_TEXT processing
    class DB,QUEUE,DRIVE,SHEETS storage
    class BRAVE external
```

---

## Component Descriptions

### Telegram Bot Layer
- **Telegram Webhook:** Receives POST requests from Telegram servers when users send messages
- **Telegram Send Message:** Sends responses back to users (acknowledgments, completions, errors)
- **Telegram Callback Handler:** Processes inline button clicks (retry actions)

### API Layer (FastAPI)
- **/webhook endpoint:** Main entry point for Telegram messages
- **URL Validator:** Checks URL format and detects content type (short vs long video)
- **Content Type Router:** Routes jobs to appropriate processing pipeline
- **/callback endpoint:** Handles retry button clicks from users
- **/health endpoint:** Service health check for monitoring

### Job Management
- **SQLite Database:** Stores job records with status tracking
- **Create Job Record:** Initializes new job with `pending` status
- **Update Job Status:** Changes status to `processing`, `complete`, or `error`
- **Get Job by ID:** Retrieves job details for processing
- **Check Duplicate URL:** Prevents re-processing of recent URLs

### Processing Services
- **Redis Queue:** Message queue for job distribution (or asyncio.Queue for single-instance)
- **Background Worker:** Long-running process that pulls jobs from queue and processes them

### Short Video Pipeline
1. **Frame Extraction Service:** Local service extracts frames from video (`:5050/short_frames`)
2. **Gemini Vision API:** Analyzes frames for text, brands, links, themes
3. **Brave Search API:** Verifies extracted links (optional)
4. **Build Markdown Summary:** Formats results into markdown document

### Long Video Pipeline
1. **Transcript Service:** Local service extracts transcript and metadata (`:5050/transcript`)
2. **Gemini Text API:** Enriches transcript with summary, key points, quotes
3. **Build Markdown Report:** Formats results into comprehensive markdown report

### Output Layer
- **Google Drive Upload:** Stores markdown files and returns shareable links
- **Google Sheets Logger:** Appends job records for historical reporting (optional)
- **Format Telegram Response:** Builds user-friendly messages with Drive links

---

## Data Flow Examples

### Success Flow (Short Video)
```
1. User sends: "https://example.com/product-demo.mp4"
2. Webhook validates URL → creates job → queues for processing
3. Bot replies: "📥 Received! Processing..."
4. Worker picks up job → extracts 10 frames
5. Gemini Vision analyzes frames → finds "5 text overlays, 3 brand mentions"
6. Brave Search verifies 2 extracted links
7. Builds markdown summary → uploads to Drive
8. Updates job status to 'complete' with Drive URL
9. Bot sends: "✅ Analysis Complete [View Report]"
```

### Error Flow with Retry
```
1. User sends video URL
2. Worker starts processing → Gemini API times out
3. Updates job status to 'error' with attempt=1
4. Bot sends: "❌ Processing Failed (Attempt 1/3) [🔄 Retry]"
5. User clicks Retry button
6. Callback endpoint resets job to 'pending', re-queues
7. Worker retries after 5s exponential backoff
8. Second attempt succeeds → completes normally
```

### Duplicate Detection Flow
```
1. User sends: "https://youtube.com/watch?v=abc123"
2. Database check finds this URL was processed 2 hours ago
3. Bot immediately replies: "✅ Already processed! [View Previous Report]"
4. No job created, no processing needed
```

---

## Key Design Decisions

### 1. Synchronous vs Asynchronous Processing
**Decision:** Immediate acknowledgment + async processing

**Rationale:**
- User gets instant feedback (<1s response)
- Processing happens in background (15-90s)
- User can continue using Telegram while waiting
- No blocking of webhook endpoint

### 2. SQLite vs PostgreSQL
**Decision:** SQLite for MVP, migration path to PostgreSQL documented

**Rationale:**
- SQLite is embedded (no separate database server)
- Sufficient for <10,000 jobs/day
- Zero configuration overhead
- Easy to backup (single file)
- Can migrate to PostgreSQL with 2-line config change if needed

### 3. Redis Queue vs asyncio.Queue
**Decision:** Redis Queue (with asyncio.Queue fallback documented)

**Rationale:**
- Redis enables multi-worker scaling
- Persistent queue (survives restarts)
- Battle-tested for production
- Minimal overhead for single-instance deployment

### 4. Direct Telegram API vs Bot Library
**Decision:** Direct API calls with `httpx`

**Rationale:**
- Full control over request/response handling
- No wrapper library version conflicts
- Simpler debugging (direct HTTP logs)
- Avoids polling overhead (webhook-based)

### 5. Separate Pipelines vs Unified Pipeline
**Decision:** Separate short/long video pipelines

**Rationale:**
- Short videos need frame extraction + Vision API
- Long videos need transcript extraction + Text API
- Different processing times and resource requirements
- Easier to optimize each pipeline independently

### 6. Error Handling Strategy
**Decision:** User-controlled retry via Telegram callbacks

**Rationale:**
- Better UX than silent auto-retry
- User confirms intent before retry
- Prevents wasting API quota on permanently broken URLs
- Provides transparency (user sees attempt count)

---

## Scalability Considerations

### Current Architecture Supports:
- **~200 jobs/hour** on 3-worker configuration
- **50 concurrent users** without degradation
- **<10,000 jobs/day** with SQLite

### Scaling Path:
1. **Horizontal Worker Scaling:** Add more worker containers (Redis supports this natively)
2. **Database Migration:** Switch to PostgreSQL when SQLite write concurrency becomes bottleneck
3. **Load Balancing:** Add nginx in front of FastAPI for multi-instance API layer
4. **Caching:** Add Redis caching for duplicate URL checks (instead of database query)

### Bottlenecks (in order of likelihood):
1. **Gemini API rate limits** (60 requests/min) → Queue with rate limiter
2. **Frame extraction service** (CPU-bound) → Scale horizontally or use GPU
3. **SQLite write concurrency** (locks on writes) → Migrate to PostgreSQL
4. **Worker count** (manual scaling) → Implement auto-scaling based on queue depth

---

## Monitoring & Observability

### Health Check Response
```json
{
  "status": "healthy",
  "timestamp": "2026-05-11T12:34:56Z",
  "uptime_seconds": 86400,
  "components": {
    "database": "healthy",
    "redis": "healthy",
    "worker": "healthy"
  },
  "queue_depth": 3
}
```

### Key Metrics to Track
- Jobs processed per hour (by content type)
- Average processing time (p50, p95, p99)
- Error rate by error type
- Queue depth over time
- Worker active/idle ratio
- API rate limit consumption (Gemini, Brave)

### Structured Log Example
```json
{
  "timestamp": "2026-05-11T12:34:56.789Z",
  "level": "info",
  "event": "job_complete",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "content_type": "short",
  "processing_time_ms": 23400,
  "frame_count": 10,
  "links_found": 3
}
```

---

## Comparison with n8n Workflow

| Aspect | n8n Workflow | Python Service |
|--------|-------------|----------------|
| **Total Nodes/Components** | 60+ visual nodes | ~15 Python modules |
| **Lines of Code** | ~2000 (JSON config) | ~500 (Python) |
| **State Management** | Google Sheets | SQLite database |
| **Observability** | Limited logging | Structured JSON logs |
| **Debugging** | Click through nodes | Standard debugger |
| **Version Control** | Single JSON file | Standard git workflow |
| **Testing** | Manual only | Unit + integration tests |
| **Performance** | ~35s / ~95s | <30s / <90s (target) |
| **Error Handling** | Callback-driven retry | State machine + exponential backoff |
| **Deployment** | n8n + Docker | Docker only |

**Winner:** Python service on all dimensions

---

## Security Features

### 1. Webhook Validation
```python
def validate_telegram_webhook(request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    return secrets.compare_digest(secret, config.TELEGRAM_WEBHOOK_SECRET)
```

### 2. SSRF Prevention
```python
BLOCKED_HOSTS = ['localhost', '127.0.0.1', '169.254.169.254']

def is_valid_url(url):
    parsed = urlparse(url)
    if parsed.hostname in BLOCKED_HOSTS:
        return False
    # Also block private IP ranges
```

### 3. Rate Limiting
```python
# 10 requests per hour per user
rate_limiter = RateLimiter(requests_per_hour=10)
```

### 4. Input Sanitization
```python
# Remove sensitive parameters before logging
def sanitize_url_for_logging(url):
    # Remove: token, key, auth, session params
```

---

## Cost Breakdown (at 1,000 jobs/month)

| Service | Usage | Cost |
|---------|-------|------|
| Gemini API | ~1M tokens/job | $15/mo |
| Google Drive | 20GB storage | $2/mo |
| VPS (if hosted) | 2GB RAM | $5/mo (optional) |
| Brave Search | 100 searches | $0 (free tier) |
| Redis | Self-hosted | $0 |
| **Total** | | **$17-22/mo** |

**Cost per job:** $0.017 - $0.022

---

## Future Architecture Extensions

### 1. Web Dashboard
```
[Web Dashboard] --> [FastAPI /api/*] --> [SQLite]
     (Vue.js)           (REST API)       (read-only)
```

### 2. Batch Processing
```
[Playlist URL] --> [Parse Playlist] --> [Create N Jobs] --> [Queue]
                                              ↓
                                        [Process in parallel]
```

### 3. Multi-Language Support
```
[Transcript] --> [Detect Language] --> [Translate] --> [Gemini Enrichment]
                     (langdetect)      (Google Translate)
```

---

## Implementation Checklist

### Phase 1: Core Infrastructure (Week 1)
- [ ] Set up FastAPI project structure
- [ ] Implement SQLite database with schema
- [ ] Create Redis queue wrapper
- [ ] Build webhook endpoint with validation
- [ ] Implement health check endpoint

### Phase 2: Processing Pipelines (Week 2)
- [ ] Integrate frame extraction service
- [ ] Implement Gemini Vision client
- [ ] Integrate transcript service
- [ ] Implement Gemini Text client
- [ ] Build markdown generation functions

### Phase 3: Output & UX (Week 3)
- [ ] Implement Google Drive uploader
- [ ] Build Telegram message formatter
- [ ] Add retry callback handler
- [ ] Implement duplicate detection
- [ ] Add structured logging

### Phase 4: Testing & Deployment (Week 4)
- [ ] Write unit tests (>80% coverage)
- [ ] Write integration tests
- [ ] Set up Docker Compose
- [ ] Deploy to local environment
- [ ] Run parallel with n8n

### Phase 5: Migration (Week 5)
- [ ] Export data from Google Sheets
- [ ] Import to SQLite
- [ ] Gradual traffic cutover
- [ ] Monitor for 1 week
- [ ] Decommission n8n

---

**Diagram Version:** 1.0  
**Last Updated:** May 11, 2026  
**Status:** Ready for Implementation
