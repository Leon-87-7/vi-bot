# Second Brain — Design Spec

**Date:** 2026-05-15  
**Status:** Approved  
**Feature:** Semantic link graph with Obsidian visual output

---

## Problem

Every processed video produces a set of extracted links that are currently discarded after the Telegram message is sent. There is no way to search across accumulated links, no memory between jobs, and no visual way to explore relationships between resources the user has encountered.

---

## Solution

A persistent link graph that accumulates every extracted URL, enriches each with a Gemini embedding, enables semantic search via Telegram and HTTP, and exports a live Obsidian vault to Google Drive where each link is a node and edges are semantic similarity relationships.

---

## Architecture

### New module: `brain.py`

Single-responsibility module. Exposes four async functions:

- `init_db(db_path)` — create `links` table if it does not exist
- `ingest_links(links, topic, source_job_id)` — store new links, embed, write `.md` to Drive
- `search_links(query, top_k=5)` — embed query, cosine similarity, return ranked results
- `rebuild_graph()` — recompute all `.md` files from scratch
- `refresh_stale_links(batch_size)` — recompute oldest-by-`updated_at` links

No Telegram or job logic inside `brain.py`. It only touches SQLite, the Gemini embedding API, and Drive.

---

## Data Model

### New SQLite table: `links`

```sql
CREATE TABLE IF NOT EXISTS links (
    id          TEXT PRIMARY KEY,       -- same YYYYMMDD_HHMMSS_XXXX format as jobs
    url         TEXT NOT NULL,
    title       TEXT,                   -- resolved title (see Title Resolution)
    topic       TEXT,                   -- video topic/summary from source job
    source_job  TEXT NOT NULL,          -- job_id that produced this link
    embedding   BLOB,                   -- numpy float32 vector, little-endian bytes
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL           -- staleness clock for refresh worker
)
```

One row per URL enforced in code (soft dedup). No DB-level UNIQUE constraint — the application skips insert if the URL already exists. This ensures one Obsidian node per URL regardless of how many videos referenced it.

---

## Title Resolution

Links extracted from short videos (reels, TikTok, Instagram) have no inherent title. Resolution order:

1. **Title present** (e.g. from YouTube description links) → use as-is
2. **No title — GitHub URL** → extract `owner/repo` from path (e.g. `github.com/vercel/next.js` → `vercel/next.js`)
3. **No title — all other URLs** → take domain minus `www.`, strip TLD suffix (e.g. `docs.tailwindcss.com` → `docs.tailwindcss`, `react.dev` → `react`, `stackoverflow.com` → `stackoverflow`)
4. Pass the extracted hint + video topic to Gemini text client with prompt:
   > "Give a short title (max 5 words) for a link to `{hint}` found in a video about `{topic}`."
5. Use Gemini's response as the stored title.

The Gemini title call reuses the existing text client in `gemini.py`. It is a ~50-token call.

---

## Ingestion Flow

Called at end of each pipeline after links are extracted:

```
brain.ingest_links(links=[...], topic="...", source_job_id="...")
```

Per link:

1. Soft dedup check — if URL exists in `links` table, skip
2. Resolve title (see Title Resolution above)
3. Build embedding document: `f"{url} {title} {topic}"`
4. Call `text-embedding-004` via Gemini SDK → serialize result as `numpy.float32.tobytes()`
5. Insert row into `links` table
6. Load all existing embeddings from SQLite into numpy matrix
7. Compute cosine similarity → top-3 by score (excluding self)
8. Write `.md` file to `GOOGLE_DRIVE_FOLDER_BRAIN` Drive folder

### Obsidian `.md` format

Filename: `{title_slug}.md` (same slug logic as Drive report files in `drive.py`)

```markdown
# {title}

**URL:** {url}
**Topic:** {topic}
**Source video:** {source_job_id}
**Added:** {created_at}

## Related
- [[{related_title_1}]]
- [[{related_title_2}]]
- [[{related_title_3}]]
```

Obsidian reads `[[...]]` wiki-links as graph edges. The Drive folder is opened as the Obsidian vault via the Google Drive desktop app.

---

## Semantic Search

### Telegram: `/find <query>`

1. Embed query with `text-embedding-004`
2. Load all embeddings from SQLite as numpy matrix
3. Cosine similarity → top-5 results sorted by score descending
4. Reply format:

```
🔗 *react* — docs.react.dev
   Topic: React hooks deep dive
   Score: 0.91

🔗 *vercel/next.js* — github.com/vercel/next.js
   Topic: Next.js App Router patterns
   Score: 0.87
```

### HTTP: `GET /links/search?q=<query>`

Same logic. Returns JSON:

```json
[
  {
    "title": "react",
    "url": "https://react.dev",
    "topic": "React hooks deep dive",
    "score": 0.91
  }
]
```

Default `top_k=5`. Accepts optional `?k=N` param (max 20).

---

## Refresh Worker

### Schedule

APScheduler cron job registered on FastAPI startup: `0 9 * * 0,3` (9 AM UTC, Sunday and Wednesday).

### Behaviour

1. Pull `BRAIN_REFRESH_BATCH` oldest links ordered by `updated_at ASC`
2. For each link: recompute top-3 similar links against full current corpus
3. Rewrite `.md` file on Drive: search Drive folder for filename match to get the file ID, then update content; create new file if not found
4. Update `updated_at` to now
5. Log: `brain.refresh done batch={n} duration={ms}ms`

Oldest-first order ensures every node is eventually refreshed as the corpus grows — no permanently stale nodes.

---

## `/rebuild-graph` Command

Telegram command and implicit `POST /links/rebuild` endpoint. Recomputes every `.md` file from scratch:

1. Load all links from SQLite
2. Rebuild full embedding matrix
3. For each link: compute top-3, write `.md` to Drive
4. Reply: "Graph rebuilt — {n} nodes written."

Use when the vault drifts (e.g. after a bulk import or Drive folder reset).

---

## Pipeline Integration

Minimal touch points — only `pipeline.py` changes:

| Pipeline | Where | Call |
|---|---|---|
| Short | After Brave links merged (step 4) | `await brain.ingest_links(links, topic, job_id)` |
| Long | After description links extracted (step 3) | `await brain.ingest_links(links, topic, job_id)` |

`main.py` startup: register APScheduler and call `brain.init_db()` to create the `links` table.

---

## New Dependencies

```
apscheduler>=3.10
numpy>=1.26
```

Added to `requirements.txt`.

---

## New Environment Variables

```
GOOGLE_DRIVE_FOLDER_BRAIN      # Drive folder ID for Obsidian vault .md files
BRAIN_REFRESH_BATCH            # default: 50 — links per Sunday/Wednesday run
GEMINI_EMBEDDING_MODEL         # default: text-embedding-004
```

Added to `.env.example`.

---

## New Telegram Commands

| Command | Behaviour |
|---|---|
| `/find <query>` | Semantic search, top-5 results |
| `/rebuild-graph` | Rewrite all Obsidian `.md` files from current corpus |

---

## Out of Scope

- Per-user link separation (all links go to one shared corpus)
- Link deduplication across videos (same URL from two videos = two rows)
- Fetching live page titles via HTTP (title comes from video context + Gemini, not page scraping)
- Graph persistence in a graph database (Neo4j, etc.) — SQLite + numpy is sufficient
- Automatic Obsidian sync beyond Drive folder write (user opens Drive folder as vault manually once)
