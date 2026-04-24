# The Government & Me — Chicago Civic Tools

A two-feature civic web app for Chicago residents, served from a single Flask process at `thegovernmentandme.tools`.

**Feature 1 — City Services Chat:** RAG chatbot answering plain-English questions about Chicago city services, drawing from a one-time scrape of chicago.gov, cps.edu, and chicagoparkdistrict.com, with live queries to the Chicago Open Data Portal (Socrata) for quantitative questions.

**Feature 2 — Legislation Search:** Plain-language search and browse of Chicago City Council legislation, powered by the ELMS public API (`api.chicityclerkelms.chicago.gov`), with Claude reranking, matter enrichment, and status context.

---

## Routes

| Route | Handler | Description |
|---|---|---|
| `GET /` | `index()` | Serves `static/landing.html` — two-card landing page |
| `GET /app` | `app_page()` | Serves `static/index.html` — chat + legislation tabs |
| `POST /chat` | `chat()` | City services Q&A (RAG + Socrata tool use) |
| `GET /health` | `health()` | Health check — scrape date + chunk count |
| `POST /feedback` | `feedback()` | Thumbs up/down + note stored in Supabase |
| `GET /legislation/search` | `legislation_search()` | Plain-language legislation search |
| `GET /legislation/matters/<record_number>` | `legislation_matter()` | Full matter detail with enrichment |
| `GET /legislation/recent` | `legislation_recent()` | Top 12 recently introduced matters |
| `GET /meetings/recent` | `meetings_recent()` | 8 most recent meetings with AI summaries |
| `GET /meetings/all` | `meetings_all()` | Up to 50 past meetings with AI summaries (saves to DB) |
| `GET /meetings/<meetingId>/matters` | `meeting_matters()` | All matters for a meeting, tagged routine/non-routine |

All HTML-serving routes set `Cache-Control: no-store` via `_no_cache()`.

---

## Static Files

```
static/
├── landing.html   ← Landing page at /. Two cards: Chat (/app) + Legislation (/app#legislation)
└── index.html     ← Main app. Two tabs: Chat (default) + Legislation
                      Tab switching is hash-based: /app#legislation auto-opens Legislation tab.
```

---

## City Services Chat (Feature 1)

### Architecture

```
User → POST /chat → embed (Voyage AI) → pgvector cosine search (Supabase)
                 → Claude Haiku: intent parse → (optional) Socrata SODA query
                 → Claude Haiku: answer with RAG context → browser
```

### Key components in `api.py`

| Component | Details |
|---|---|
| Embedding model | `voyage-multilingual-2` (Voyage AI API), 1024-dim, `input_type=query/document` |
| Vector store | pgvector on Supabase, cosine similarity `<=>`, threshold 0.35 |
| LLM | `claude-haiku-4-5-20251001` primary, `claude-sonnet-4-6` fallback on 529 |
| Intent parsing | `_parse_intent()` — lightweight structured Claude call before main RAG call |
| Live data | `query_socrata()` via Claude tool use — 4 datasets (crime, permits, licenses, 311) |
| Community areas | Loaded from `Boundaries_-_Community_Areas*.csv` at startup — name↔number mapping |
| Overload handling | `_claude_create()` — 3 retries with 2s/4s backoff on HTTP 529, then Sonnet fallback |
| Conversation logging | `upsert_turn()` — every turn stored in Supabase `sessions` table (JSONB) |
| Multilingual | 7 languages (en, es, pl, zh, ar, tl, hi); `voyage-multilingual-2` handles queries natively |

### Socrata datasets

| Key | Dataset ID | Queryable fields |
|---|---|---|
| `business_licenses` | `r5kz-chrr` | license type, ward, community area, date |
| `building_permits` | `ydr8-5enu` | permit type, ward, community area, date |
| `crime` | `ijzp-q8t2` | primary type, community area, date, arrest |
| `311_requests` | `v6vf-nfxy` | service request type, community area, date |

### Data model (Supabase / PostgreSQL)

- **`chunks`** — scraped page chunks with `embedding vector(1024)`, url, title, level1/2/3 tags
- **`scrape_info`** — scrape date, model, page/chunk counts
- **`sessions`** — `session_id`, `lang`, `conversation` (JSONB), `feedback`, `feedback_note`
- **`source_debug_log`** — retrieved vs. used vs. filtered URLs per request
- **`data_query_log`** — every Socrata tool call with dataset, where/select clauses, row count
- **`plain_language_titles`** — `record_number → plain_title`; Claude translations cached across restarts
- **`attachment_summaries`** — `url_hash (md5) → summary`; PDF summaries at 5th-grade level
- **`meeting_summaries`** — `meeting_id → summary`; ≤50-word meeting summaries

---

## Legislation Search (Feature 2)

### ELMS API

Base URL: `https://api.chicityclerkelms.chicago.gov`  
No auth required. Helper: `_elms_get(path, params)` — plain `requests.get`.

Endpoints used:

| Path | Purpose |
|---|---|
| `/search?search=<q>&top=N` | Full-text search across matters |
| `/matter/recordNumber/<id>` | Full matter record — includes `attachments[]`, `actions[]`, `sponsors[]` |
| `/matter/<matterId>` | Same, by internal UUID |
| `/meeting-agenda?search=<body>&top=200` | Find meetings by committee name |
| `/meeting-agenda/<meetingId>` | Full meeting record — location, videoLink, files[] |

Key ELMS field notes:
- `subStatus` — capital S (not `substatus`)
- `attachments` — direct matter attachments (legislation PDFs, reports); this is what surfaces in "Related Documents"
- Meeting `files[]` exist but are **not** surfaced in the UI (they belong to the full agenda, not the matter)

### Search pipeline (`legislation_search`)

1. `_elms_get("/search", {"search": q, "top": 25})`
2. Filter boilerplate via `_is_boilerplate()` (damage claims, parking tickets, tax levies, etc.)
3. `_claude_rerank(q, matters)` — Claude Haiku scores each matter 1–10 for relevance; drops scores < `RELEVANCE_CUTOFF` (4)
4. Return top 10 with slim fields: `recordNumber`, `title`, `status`, `subStatus`, `type`, `introductionDate`, `controllingBody`
5. `_plain_language_titles(matters)` — batches uncached titles to Claude Haiku; returns `{"recordNumber": "plain title"}` dict; cached in module-level `_plain_language_cache`

### Routine/non-routine classification

`_classify_routine(matterType, matterTitle)` returns `True` for routine (agreed-calendar) items:
- `matterType` in `{"claim", "communication", "report", "oath"}` → routine
- `_is_boilerplate()` keyword match → routine
- Anything else → non-routine

The `agreedCalendar` field (`"YES"`/`"NO"`) is available on matters from the ELMS `/search` endpoint and full matter records, but is **not** present on meeting agenda items from `/meeting-agenda/{meetingId}`. The classification function above is used for meeting agenda items.

### Matter enrichment (`_enrich_matter`)

Called on `GET /legislation/matters/<record_number>`.

1. **Meeting details** — for each action, search meetings by `(actionByName, date)`, fetch full meeting record for `location` and `videoLinks`
2. **Status context** — sets `matter["statusContext"]` from `_STATUS_CONTEXT` dict based on `status`/`subStatus`:
   - `in_committee_active` / `in_committee_stale` (>180 days) / `held_in_committee` / `referred` / `passed` / `failed` / `withdrawn` / `tabled`
3. **Action-level context** — referral actions get `action["statusContext"]` = Rule 41 explanation
4. **Type description** — `matter["typeDescription"]` from `_LEGISLATION_TYPES` dict (ordinance, resolution, order, report, etc.)
5. **Direct attachments** — `matter["matterAttachments"]` = `matter["attachments"]` from ELMS (legislation PDFs, etc.). For `.pdf` URLs, `_pdf_summary()` is called to add a `summary` field — cached in `attachment_summaries` table.
6. **Committee chair** — fuzzy match `matter.controllingBody` against `_COMMITTEE_CHAIRS` dict; sets `matter["committeeChair"]`
7. **What can you do?** — `matter["whatCanYouDo"]` list: contact alderperson link + committee chair contact if in committee
8. **Plain language title** — `matter["plainLanguageTitle"]` via `_plain_language_titles()`

### Static data in `api.py`

| Constant | Content |
|---|---|
| `_COMMITTEE_CHAIRS` | 20 committees → `{"name": "Ald. ...", "ward": N}` — Sep 2025 data |
| `_LEGISLATION_TYPES` | 8 matter types → plain English description |
| `_STATUS_CONTEXT` | 8 status keys → plain English explanation strings |
| `_plain_language_cache` | Module-level dict, `recordNumber → plain title`; fully preloaded from DB at startup in `load_resources()`, so API is only called for new matters |
| `RELEVANCE_CUTOFF` | `4` — minimum Claude rerank score to include in results |

### Frontend (Legislation tab in `static/index.html`)

- **Tab switching** — `switchTab("legislation")` called on hash `#legislation` at page load
- **Browse on open** — `loadRecentMeetings()` called once per tab open via `legRecentLoaded` flag; fetches `/meetings/recent` and renders 8 meeting cards
- **All meetings** — "Browse all meetings →" button at bottom of recent meetings calls `loadAllMeetings()` → fetches `/meetings/all` (up to 50, cached in `legAllMeetings`); same card format; back button returns to recent meetings
- **Meeting drill-down** — `loadMeetingMatters(meetingId, label)` fetches `/meetings/{id}/matters`; re-uses `renderResults()` with routine badge; back button returns to whichever meeting list was active
- **Meeting cards** — show date, 50-word AI summary, non-routine/routine counts
- **Search** — `searchLegislation()` → `renderResults()` → result cards with plain language subtitle (italic), status badge (Active/Stale for In Committee), meta line
- **Detail view** — `loadMatter()` → `renderMatterDetail()`: plain language subtitle, type description block, status context callout, action timeline with location + video, "Related Documents" from `matterAttachments`, "What can you do?" section
- **Back navigation** — context-aware: `legResultSource` ("search" | "meeting" | "all-meetings") controls where back buttons lead; `legShowingAllMeetings` flag tracks whether all-meetings view is active
- **Legislation is English-only** — ELMS API content is English; existing chat multilingual support is untouched

---

## Project Structure

```
govt-chatbot/
├── api.py                              ← Flask backend (chat RAG + legislation search)
├── scrape_and_index.py                 ← One-time data pipeline (scrape → embed → Supabase)
├── gunicorn.conf.py                    ← Gunicorn worker/timeout config
├── schema.sql                         ← Supabase table definitions
├── dataset_schemas.json               ← Socrata dataset field schemas for tool use
├── Boundaries_-_Community_Areas*.csv  ← Chicago community area number↔name mapping
├── static/
│   ├── landing.html                   ← Landing page (/ route)
│   └── index.html                     ← Main app (chat + legislation tabs)
├── vectors/
│   └── scraped_pages.json             ← Page cache for chicago.gov (avoids re-scraping)
├── requirements.txt
├── render.yaml                        ← Render deployment config
└── .env                               ← ANTHROPIC_API_KEY, VOYAGE_API_KEY, DATABASE_URL, SOCRATA_APP_TOKEN
```

---

## Setup & Running

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set API keys
python scrape_and_index.py   # one-time: scrape + embed + write to Supabase
python api.py              # → http://localhost:5001
```

Landing page at `/`, chat + legislation at `/app`.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude Haiku for chat, reranking, plain language titles |
| `VOYAGE_API_KEY` | Yes | voyage-multilingual-2 embeddings |
| `DATABASE_URL` | Yes | Supabase PostgreSQL connection string |
| `SOCRATA_APP_TOKEN` | Optional | Raises Socrata rate limit from 1 req/s → 10 req/s |

---

## Cost Model

| Component | Per query |
|---|---|
| Voyage AI embed (query) | ~$0.00004 |
| Claude Haiku input (~3 200 tok) | ~$0.0026 |
| Claude Haiku output (~250 tok) | ~$0.001 |
| Legislation rerank (Haiku, ~500 tok) | ~$0.0004 |
| Plain language title (Haiku, ~200 tok) | ~$0.0002 |
| **Chat query total** | **~$0.004** |
| **Legislation search total** | **~$0.001** |

Supabase free tier covers vector search and session storage at low traffic. Render hobby plan (~$7/month) covers hosting.
