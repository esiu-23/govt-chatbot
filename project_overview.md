# Chicago City Services RAG Chatbot
Ignore the writing/ folder when querying the codebase.

A Retrieval-Augmented Generation (RAG) chatbot that lets users ask plain-English
questions about Chicago city services. Knowledge comes from a one-time scrape of:

- `chicago.gov/city/en/depts.html` — all city department pages
- `cps.edu` — Chicago Public Schools (up to 60 pages, one level from home)
- `chicagoparkdistrict.com` — Chicago Park District (up to 60 pages, one level from home)

Every answer carries a disclaimer with the exact scrape date so users know when
the data was last captured.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Data Pipeline DAG](#data-pipeline-dag)
3. [Request / Response Flow](#request--response-flow)
4. [Data Model](#data-model)
5. [Project Structure](#project-structure)
6. [Setup & Running](#setup--running)
7. [Cost Model](#cost-model)
8. [Design Decisions](#design-decisions)
9. [Limitations & Future Work](#limitations--future-work)

---

## Architecture Overview

```mermaid
flowchart LR
    subgraph Offline["Offline — run once"]
        A([chicago.gov/depts]) -->|requests + BS4| B[Raw HTML]
        B -->|clean + chunk + classify| C[Text Chunks\n+ level1/2/3 tags]
        C -->|Voyage AI API| D[1024-d Vectors]
        D -->|psycopg2 + pgvector| E[(Supabase\nPostgreSQL)]
    end

    subgraph Online["Online — per user request"]
        G([User Browser]) -->|POST /chat| H[Flask API]
        H -->|embed question| I[Voyage AI API]
        I -->|pgvector <=> cosine search| E
        E -->|top-5 chunks ≥ 0.35 similarity| H
        H -->|question + context + history| K[Claude Haiku API]
        K -->|answer + SOURCES line| H
        H -->|answer + filtered sources + disclaimer| G
        H -->|upsert turn| E
    end
```

**Key principle:** The LLM (Claude) never touches the internet at inference time.
It only sees text retrieved from Supabase via pgvector cosine search.

---

## Data Pipeline DAG

Steps that run when you execute `python scrape_and_index.py`.

```mermaid
flowchart TD
    S([Start]) --> L1

    L1["1 · Discover links\nGET chicago.gov/depts.html + cps.edu + chicagoparkdistrict.com\nparse hrefs one level deep"]
    L1 --> L2

    L2["2 · Scrape pages\nGET each dept URL\nstrip nav/footer/scripts\nextract main body text\n~0.5 s delay between requests\ncache written to vectors/scraped_pages.json"]
    L2 --> L3

    L3["3 · Chunk text\nSliding window\nmax 2 000 chars / chunk\n300-char overlap\nbreak on newlines\nclassify level1 (URL slug) + level2 (title) + level3 (keywords)"]
    L3 --> L4

    L4["4 · Embed chunks\nvoyage-multilingual-2 API\nbatch_size=64\n→ 1024-dim float32 vectors (L2-normalised)"]
    L4 --> L5

    L5["5 · Write to Supabase\nDELETE old scrape_info + chunks\nINSERT scrape_info (date, model, counts)\nbulk INSERT chunks (text + metadata + embedding vector)\nvia psycopg2 execute_values"]
    L5 --> E([Done])

    style S fill:#003F87,color:#fff
    style E fill:#003F87,color:#fff
```

---

## Request / Response Flow

Sequence of calls for a single user question.

```mermaid
sequenceDiagram
    actor User
    participant Browser
    participant Flask as Flask API (api.py)
    participant Voyage as Voyage AI API
    participant Supa as Supabase / pgvector
    participant Claude as Claude Haiku (Anthropic API)

    User->>Browser: Types question, clicks Send
    Browser->>Flask: POST /chat { "question", "lang", "history", "session_id", "clarify_count" }

    Flask->>Voyage: embed(question, input_type="query")
    Voyage-->>Flask: 1024-dim query vector

    Flask->>Supa: SELECT ... FROM chunks ORDER BY embedding <=> q_vec LIMIT 5
    Supa-->>Flask: top-5 rows with cosine similarity scores

    Note over Flask: Filter rows where similarity < 0.35

    Flask->>Claude: system prompt + retrieved context + conversation history + question
    Claude-->>Flask: answer text ending with "SOURCES: <urls>"

    Note over Flask: Strip SOURCES line; filter sources list to URLs Claude cited

    Flask->>Supa: upsert_turn (sessions table) + log_source_debug
    Flask-->>Browser: { type, answer, sources[], scrape_date, disclaimer }
    Browser-->>User: Renders answer bubble + source links + disclaimer
```

---

## Data Model

All data lives in **Supabase (PostgreSQL)** with the `pgvector` extension.

```mermaid
erDiagram
    SCRAPE_INFO {
        serial  id            PK
        string  scrape_date       "ISO date, e.g. 2025-04-11"
        string  model             "embedding model name"
        int     total_pages       "pages successfully scraped"
        int     total_chunks      "chunks created"
    }

    CHUNKS {
        serial  rowid         PK
        string  id                "url__chunk_N"
        string  url               "full https URL"
        string  title             "HTML <title> text"
        string  text              "raw chunk text (≤ 2 000 chars)"
        int     chunk_index       "position within page (0-based)"
        string  level1            "top category (Public Safety, Education, etc.)"
        string  level2            "department name from page title"
        string  level3            "content type: how_to | contact | programs | overview"
        vector  embedding         "1024-dim float32 pgvector column"
    }

    SESSIONS {
        serial  id            PK
        string  session_id    UK   "UUID from browser crypto.randomUUID()"
        string  lang               "language code (en, es, pl, zh, ar, tl, hi)"
        jsonb   conversation       "array of {role, content, type, sources, timestamp}"
        string  feedback           "up | down | null"
        string  feedback_note      "free-text feedback"
        timestamp last_updated
    }

    SOURCE_DEBUG_LOG {
        serial  id            PK
        string  session_id
        string  question
        jsonb   retrieved_urls     "all pgvector results"
        jsonb   used_urls          "URLs Claude cited in SOURCES line"
        jsonb   filtered_urls      "final sources returned to browser"
        int     fallback_used      "1 if fell back to all retrieved sources"
        timestamp created_at
    }

    SCRAPE_INFO  ||--o{ CHUNKS         : "produced"
    SESSIONS     ||--o{ SOURCE_DEBUG_LOG : "has debug rows"
```

---

## Project Structure

```
govt-chatbot/
├── scrape_and_index.py   ← one-time data pipeline (run before first use)
├── api.py                ← Flask backend + RAG logic
├── gunicorn.conf.py      ← gunicorn worker/timeout config
├── static/
│   └── index.html        ← two-column chat UI (vanilla JS): left info panel (1/3) + right chat (2/3)
├── vectors/
│   └── scraped_pages.json ← page cache (avoids re-scraping chicago.gov)
├── requirements.txt
├── .env                  ← ANTHROPIC_API_KEY + VOYAGE_API_KEY + DATABASE_URL (not committed)
├── .env.example
└── project_overview.md   ← this file
```

> **Note:** There is no local `vectors.db`, `index.faiss`, `metadata.json`, or `conversations.db`.
> All persistent data (chunks, embeddings, sessions, feedback) lives in Supabase.

---

## Setup & Running

### 1 · Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2 · Add your API keys

```bash
cp .env.example .env
# edit .env and set:
#   ANTHROPIC_API_KEY=sk-ant-...
#   VOYAGE_API_KEY=pa-...
#   DATABASE_URL=postgresql://...  ← Supabase connection string
```

### 3 · Scrape & index (run once)

```bash
python scrape_and_index.py
```

This downloads ~40 pages from chicago.gov plus up to 60 pages each from
cps.edu and chicagoparkdistrict.com, chunks, classifies, and embeds them,
then writes all data directly to Supabase. Takes 5–10 minutes on first run
(scraping + embedding). Subsequent re-runs reuse `vectors/scraped_pages.json`
page cache for chicago.gov.

### 4 · Start the API server

```bash
python api.py
# → http://localhost:5001

# or with gunicorn (production):
# gunicorn -c gunicorn.conf.py api:app
```

### 5 · Open the chat UI

Navigate to [http://localhost:5001](http://localhost:5001) in your browser.

---

## Cost Model

| Component | Cost |
|---|---|
| Embeddings (scrape, ~500 chunks) | ~$0.05 one-time — Voyage AI API ($0.00012/1K tokens) |
| Embeddings (queries) | ~$0.00004/query — Voyage AI API |
| Vector search (pgvector on Supabase) | **~$0** — included in Supabase free tier |
| Claude Haiku — input (~3 200 tok/query) | ~$0.0026/query |
| Claude Haiku — output (~250 tok/query) | ~$0.001/query |
| **Per query total** | **~$0.0036** |

At 100 visitors/week averaging 3 questions each:

| Period | Queries | API cost |
|---|---|---|
| Week | 300 | ~$1.08 |
| Month | 1 300 | ~$4.68 |

Hosting: $0 locally or on Supabase free tier, ~$5–7/month on a small VPS (Render, Railway,
DigitalOcean).

---

## Design Decisions

| Decision | Choice | Reason |
|---|---|---|
| Embedding model | `voyage-multilingual-2` via Voyage AI API | Multilingual support; no local model download; 1024-dim vectors |
| Query vs document embedding | `input_type="query"` / `"document"` | Voyage supports asymmetric retrieval natively |
| Vector store | **pgvector on Supabase** | Replaces local FAISS — persistent, no startup load time, enables filtering by metadata columns; cosine via `<=>` operator |
| Score threshold | `similarity ≥ 0.35` | Drops clearly irrelevant chunks before sending to Claude |
| Connection pooling | `ThreadedConnectionPool(min=2, max=10)` | Reuses DB connections across concurrent Flask threads |
| Conversation storage | **Supabase `sessions` table (JSONB)** | Replaces local `conversations.db` SQLite — single source of truth, queryable |
| LLM | Claude `claude-haiku-4-5-20251001` | Fastest + cheapest Claude model; sufficient for grounded Q&A |
| Web framework | Flask + gunicorn | Minimal overhead; threaded workers handle concurrent requests |
| Scrape strategy | One-time with date disclaimer + page cache | Government content changes slowly; simpler than a scheduler; transparent to users |
| Chunk classification | level1 (URL slug), level2 (title), level3 (keywords) | Enables future filtered search by category or content type |
| Chunk size | 2 000 chars / 300-char overlap | Balances context completeness vs. prompt token cost |

---

## Limitations & Future Work

- **Stale data** — re-run `scrape_and_index.py` manually to refresh. A cron job
  could automate this monthly.
- **JavaScript-rendered pages** — `requests` + BeautifulSoup only sees server-rendered
  HTML. Any dept page that loads content via JS will be partially scraped.
  Replace with `playwright` if needed.
- **Inline URL linkification** — answer text is passed through `linkify()` before being inserted as `innerHTML`. The function HTML-escapes the text, then wraps bare `https?://` URLs in `<a target="_blank">` tags so any chicago.gov links Claude mentions become clickable directly inside the chat bubble.
- **Source filtering** — Claude appends a `SOURCES: <urls>` line to every answer. The backend strips that line from the displayed text and uses it to filter the pgvector-retrieved sources list down to only the pages Claude actually drew from.
- **Conversation logging** — every `/chat` turn (question + answer, including clarifications) is upserted into the `sessions` table in Supabase. Schema: `session_id, lang, conversation (JSONB array), feedback, feedback_note, last_updated`. The `session_id` is a UUID generated once per page load in the browser (`crypto.randomUUID()`), allowing multi-turn sessions to be grouped.
- **Source debug logging** — every request logs retrieved vs. used vs. filtered URLs to `source_debug_log` for observability.
- **Session memory** — the frontend maintains a `conversationHistory` array (`[{role, content}]`) in JavaScript memory. Each turn is appended after a successful response. The full history is sent to `/chat` on every request so Claude sees the whole conversation. History is lost when the tab is closed (no persistence). The frontend also tracks `consecutiveClarifyCount`; after 1 consecutive clarification Claude is instructed to answer or say "I don't know" with relevant links.
- **Multilingual** — Language selector (English, Español, Polski, 中文, العربية, Tagalog, हिन्दी) is live. The frontend passes `lang` to `/chat`; Claude is instructed to reply in the chosen language. The embedding model (`voyage-multilingual-2`) handles multilingual queries natively. RTL layout is applied automatically for Arabic.
- **No auth** — fine for a public civic tool; add API key middleware if you
  want to rate-limit or restrict access.
- **Feedback strip** — thin banner below the chat input showing "Find this helpful? 👍 👎". Feedback type and optional note are stored in the `sessions` table in Supabase via the `/feedback` endpoint.
