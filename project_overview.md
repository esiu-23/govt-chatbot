# The Government & Me — Chicago & Illinois Civic Tools

** Notes for Substack: eLMS data is incomplete/buggy, there are meetings where the matter is not tied ot the meeting. So had to use Claude to parse the Agenda to find the discussed matters & link them to the relevant meetings.

Substituted matters. May be under original ID or with S added to the ID, and eLMS API isn't always consisted with how they tie the actions to the ID.

**

A civic web app for Chicago residents, served from a single Flask process at `thegovernmentandme.tools`. Phase 1 refactor complete: `api.py` is now a 17-line shim; all logic lives in `app/`.

**Feature 1 — City & State Services Chat:** RAG chatbot answering plain-English questions about Chicago city *and* Illinois state services. Draws from chicago.gov, cps.edu, chicagoparkdistrict.com, and Illinois state sites (illinois.gov, ides.illinois.gov, dhs.state.il.us). Live queries to both the Chicago Open Data Portal and Illinois Open Data Portal (data.illinois.gov) for quantitative questions. Chat proactively identifies whether a service is city-run, state-run, or an independent authority.

**Feature 2 — Chicago Legislation Search:** Plain-language search and browse of Chicago City Council legislation, powered by the ELMS public API (`api.chicityclerkelms.chicago.gov`), with Claude reranking, matter enrichment, status context, and "Submit public comment" action (with ELMS deadline) for non-final matters.

**Feature 3 — Illinois Legislation Search:** Plain-language search and browse of Illinois General Assembly bills, powered by the Legiscan API (`api.legiscan.com`), with Claude reranking, bill enrichment, status context, and sponsor information.

---

## Routes

| Route | Handler | Description |
|---|---|---|
| `GET /` | `index()` | Serves `static/landing.html` — landing page with tools + Analyses section |
| `GET /app` | `app_page()` | Serves `static/index.html` — chat + legislation tabs |
| `GET /analyses` | `analyses_index()` | Redirects to `/` |
| `GET /analyses/who-controls-chicago` | `who_controls_chicago()` | Serves analysis article: org chart + ward map |
| `POST /chat` | `chat()` | City services Q&A (RAG + Socrata tool use) |
| `GET /health` | `health()` | Health check — scrape date + chunk count |
| `POST /feedback` | `feedback()` | Thumbs up/down + note stored in Supabase |
| `GET /legislation/search` | `legislation_search()` | Chicago legislation search |
| `GET /legislation/matters/<record_number>` | `legislation_matter()` | Chicago matter detail with enrichment |
| `GET /legislation/recent` | `legislation_recent()` | Top 12 recently introduced Chicago matters |
| `GET /illinois/legislation/search` | `il_legislation_search()` | Illinois state bill search (Legiscan) |
| `GET /illinois/legislation/bills/<bill_id>` | `il_legislation_bill()` | Illinois bill detail with enrichment |
| `GET /illinois/legislation/recent` | `il_legislation_recent()` | Recent Illinois state bills |
| `GET /meetings/recent` | `meetings_recent()` | 8 most recent meetings with AI summaries |
| `GET /meetings/all` | `meetings_all()` | Up to 50 past meetings with AI summaries (saves to DB) |
| `GET /meetings/<meetingId>/matters` | `meeting_matters()` | All matters for a meeting, tagged routine/non-routine |
| `POST /subscribe/meetings` | `subscribe_meetings()` | Subscribe email to a committee/council body |
| `POST /subscribe/matters` | `subscribe_matters()` | Track a specific piece of legislation by record number |
| `GET /subscribe/confirm/<token>` | `confirm_subscription()` | Double opt-in confirmation |
| `GET /unsubscribe/<token>` | `unsubscribe()` | Remove subscription (meetings or matters) |
| `GET /subscribe/bodies` | `subscribe_bodies()` | List of subscribable council bodies |

All HTML-serving routes set `Cache-Control: no-store` via `_no_cache()`.

### Deep linking
`/app?matter=O-2025-1234` — auto-opens the legislation tab and loads the given matter. Used in email links.
`/app?meeting=<meetingId>` — auto-opens the legislation tab and loads the given meeting's matters/documents. Used in meeting email links.

---

## Standalone Analysis Scripts

### 311 & Multi-Source Topic Analysis (`explore_311_data.ipynb`, `analyze_311_topics.ipynb`, `analyze_multisource_topics.ipynb`)

Jupyter notebooks for hierarchical BERTopic analysis of Chicago open data sources (2019–2026), producing interactive visualizations and CSVs for future legislative-topic comparison by community area.

**Notebooks:**
- `explore_311_data.ipynb` — Exploratory: fetches sample records, checks field completeness, cross-tabs sr_type × community_area × year. Key finding: 110 unique sr_types, no free-form text, `community_area` 99.9% complete.
- `analyze_311_topics.ipynb` — Full BERTopic pipeline for 311 data only: 12 major topics from 109 unique sr_type strings.
- `analyze_multisource_topics.ipynb` — Extends analysis to 4 Socrata datasets: 311 requests (12 topics), crime (6), building permits (5), business licenses (10).

**Key technical notes:**
- Geographic unit: `community_area` (77 stable zones; Chicago redistricted wards in May 2023, making ward data inconsistent 2019–2026)
- Scale: Socrata `$group` aggregation returns sr_type × community_area × year counts (~66K rows); BERTopic runs only on unique type strings (~6–110 per source)
- BERTopic pattern: `embedding_model=None` + pre-computed embeddings passed to `fit_transform(..., embeddings=embs)` — avoids BERTopic 0.17 / sentence-transformers 2.x incompatibility (`StaticEmbedding` import failure)
- Filter: "311 INFORMATION ONLY CALL" excluded from 311 analysis (catch-all, 35.5% of records)
- Kernel: `govt-chatbot-311` (registered in project venv)

**Outputs (`311_analysis/`):**
- Per-source CSVs: `311_topics_by_community.csv`, `crime_topics_by_community.csv`, `building_permits_topics_by_community.csv`, `business_licenses_topics_by_community.csv`
- `community_dominant_topics.csv` — wide format, one row per community area, dominant topic per source
- `multisource_topic_summary.csv` — all topics across all sources with cohesion scores
- Interactive HTML: sunburst with community area dropdown, heatmap, trend lines, group strength chart — per source + combined

**Datasets used:**
| Source | Dataset ID | Field used |
|---|---|---|
| 311 requests | `v6vf-nfxy` | `sr_type` |
| Crime | `ijzp-q8t2` | `primary_type` |
| Building permits | `ydr8-5enu` | `permit_type` |
| Business licenses | `r5kz-chrr` | `license_description` |

**Future use:** Join `*_topics_by_community.csv` files to Chicago legislation topics on `community_area + year` to compare resident complaints vs. alderperson legislation by neighborhood.

---

### `analyze_library_holds.py`
Proxy analysis for CPL hold delivery time savings. Fetches three Chicago Open Data datasets (library locations `x8fc-8rcq`, 2026 holds filled `xgw6-5ftq`, 2024 circulation `utjc-493b`) and computes for each branch whether a nearby higher-circulation branch exists within walking distance.

Run: `python analyze_library_holds.py`
Output: console summary + `library_holds_analysis.csv`

Key finding (Jan–Apr 2026): 25 of 80 CPL branches have a higher-circulation branch within a 30-min walk, accounting for 17.9% of total holds (78,539). If patrons walked there instead of waiting for hold delivery, they could save an estimated 3–7 days per hold.

Caveats: uses circulation volume as a proxy for copy availability; no per-item data; patron home location not known; hold delivery time is assumed from CPL published estimate.

---

## Static Files

```
static/
├── landing.html              ← Landing page at /. Tools cards + Analyses section
├── index.html                ← Main app. Three tabs: Chat (default) + Chicago Legislation + Illinois Legislation
│                                Tab switching is hash-based: /app#legislation or /app#il-legislation
└── analyses/
    └── who-controls-chicago.html  ← Analysis 1: interactive D3 org chart + Leaflet ward map
```

**Analyses section (branch: `analysis-positions-diagram`):**
Standalone HTML articles served from `static/analyses/`. No backend calls — all data fetched client-side from Chicago Open Data (Socrata public APIs). Libraries: D3.js v7 (org chart), Leaflet.js 1.9 (ward map).

Analysis 1 — "Who Controls Chicago?" (`/analyses/who-controls-chicago`):
- D3 collapsible horizontal org chart: City of Chicago → elected officials → appointed department heads → dept details
- Data sources: employees `xzkq-xp2w`, budget `6694-f78c`, contracts `rsxa-ify5`, vendor payments `pkr3-4xv7`, TIF projects `mex4-ppfc`
- Spending streams table: shows all money channels (budget, contracts, vendor payments, TIF, delegate agencies) and who controls each
- Leaflet ward map: 50 Chicago wards with TIF district overlay (`fz5x-7zak`), toggle control
- Missing data shown explicitly as "— not in open data" (never hidden)

---

## City Services Chat (Feature 1)

### Architecture

```
User → POST /chat → embed (Voyage AI) → pgvector cosine search (Supabase)
                 → Claude Haiku: intent parse → (optional) Socrata SODA query
                 → Claude Haiku: answer with RAG context → browser
```

### Key components (post-refactor)

All logic now in `app/`. `api.py` is a shim: `from app import create_app; app = create_app()`.

**Module layout:**
```
app/
  __init__.py          create_app() factory, middleware, blueprint registration
  config.py            constants (MODEL_NAME, CLAUDE_PRIMARY, TOP_K, etc.)
  db.py                _pool, _db() context manager, _ipv4_connect_params()
  claude_client.py     Anthropic client, _claude_create() with retry/fallback
  prompts.py           SYSTEM_PROMPT_DOMAIN, SYSTEM_PROMPT_DATA, disclaimers
  session_store.py     upsert_turn, save/get_last_intent, log_* helpers
  resources.py         load_resources() — called by gunicorn post_fork
  data_sources/
    __init__.py        ContextSource + ToolSource protocols; CONTEXT_SOURCES/TOOL_SOURCES lists
    rag.py             RAGSource — voyage embed + pgvector cosine search
    socrata.py         SocrataSource — Chicago DATASETS, query_socrata, parse_intent, community areas
    illinois_socrata.py IllinoisSocrataSource — IL state datasets (IDES, IDOT, IDHS, ISBE, IDPH)
    elms.py            ELMS helpers — get_enriched_matter (DB-first), enrich_matter, plain_language_titles, meeting_summary, link_agenda_matters, etc.
    legiscan.py        Legiscan API — IL state bills, enrich_bill, plain_language_titles, bill summaries
  email/
    templates.py       render_summary_email, render_agenda_email, render_matter_update_email
    sender.py          send_email() — Resend wrapper (RESEND_API_KEY env var)
  routes/
    pages.py           /, /app, /health
    chat.py            /chat, /feedback
    legislation.py     /legislation/* (Chicago City Council)
    illinois_legislation.py /illinois/legislation/* (IL General Assembly via Legiscan)
    meetings.py        /meetings/* — DB-first: reads meeting_summaries + known_meetings before calling ELMS
    subscriptions.py   /subscribe/*, /unsubscribe/<token>
  scheduler.py         Two APScheduler jobs:
                       • sync_meeting_schedule() — daily; fetches lightweight meeting list from ELMS
                         (past 30 days + next 90 days), upserts known_meetings, registers DateTrigger
                         one-shot polls at meeting start/end, sends "new meeting" alerts to subscribers
                       • check_and_send_meeting_emails() — called by DateTrigger + 4h safety net;
                         queries known_meetings (not ELMS), fetches agenda content for meetings that need it,
                         writes meeting_items + matter_detail_cache + meeting_summaries, sends emails
                       • check_and_send_matter_updates() — every 4h; polls matter status for confirmed
                         matter_subscriptions, sends status-change emails
prepopulate_2026.py   One-time script: pre-populates all DB caches for 2026 meetings + matters.
                       Phase 1 — fetches every past 2026 meeting, upserts known_meetings, caches
                       meeting_items, generates meeting_summaries, and enriches all non-routine matters
                       into matter_detail_cache + plain_language_titles + attachment_summaries.
                       Phase 2 — collects all record numbers from meeting_items JOIN known_meetings
                       for 2026 meetings (catches any-year matters that appeared on a 2026 agenda,
                       e.g. 2025-introduced substitute ordinances), enriches uncached ones.
                       Run once after deploy:
                         DATABASE_URL=... ANTHROPIC_API_KEY=... python prepopulate_2026.py
```

**To add a new data source for /chat:**
- `ContextSource`: create `app/data_sources/new.py`, implement `name` + `fetch(question, embedding)`, append to `CONTEXT_SOURCES` in `resources.py`
- `ToolSource`: implement `name` + `tool_definition()` + `execute(tool_input)`, append to `TOOL_SOURCES`

**To add a new feature/route:** create `app/routes/new.py` Blueprint, register in `app/__init__.py`.

### Key components (legacy reference)

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
| Multilingual | 7 languages (en, es, pl, zh, ar, tl, hi) supported in backend/`UI_STRINGS`; `voyage-multilingual-2` handles queries natively. **Frontend dropdown temporarily restricted to English only** — non-English `<option>`s in `#lang-select` (`static/index.html`) are `disabled` with a "(English only for now)" note (`#lang-note`); re-enable by removing the `disabled` attributes and the note when other languages are ready to ship |

### Chicago Socrata datasets (data.cityofchicago.org)

| Key | Dataset ID | Queryable fields |
|---|---|---|
| `business_licenses` | `r5kz-chrr` | license type, ward, community area, date |
| `building_permits` | `ydr8-5enu` | permit type, ward, community area, date |
| `crime` | `ijzp-q8t2` | primary type, community area, date, arrest |
| `311_requests` | `v6vf-nfxy` | service request type, community area, date |

### Illinois Socrata datasets (data.illinois.gov)

| Key | Dataset ID | Description |
|---|---|---|
| `il_unemployment_claims` | `7set-k26h` | IDES weekly unemployment claims |
| `il_traffic_crashes` | `8mzk-wtze` | IDOT statewide crash data |
| `il_medicaid_enrollment` | `ytpe-fmkj` | IDHS Medicaid/CHIP enrollment |
| `il_school_report_card` | `fmkp-yad6` | ISBE school performance metrics |
| `il_food_inspections` | `t34d-dfxb` | IDPH licensed food establishment inspections |
| `il_public_health_stats` | `dm5n-v6ku` | IDPH public health statistics |

### Data model (Supabase / PostgreSQL)

- **`chunks`** — scraped page chunks with `embedding vector(1024)`, url, title, level1/2/3 tags, `source_scope` ('city' | 'state_il')
- **`scrape_info`** — scrape date, model, page/chunk counts
- **`sessions`** — `session_id`, `lang`, `conversation` (JSONB), `feedback`, `feedback_note`
- **`source_debug_log`** — retrieved vs. used vs. filtered URLs per request
- **`data_query_log`** — every Socrata tool call with dataset, where/select clauses, row count, `query_scope`
- **`il_plain_language_titles`** — `bill_id → plain_title`; IL bill translations cached across restarts
- **`il_bill_detail_cache`** — `bill_id → {status, data JSONB}`; 1-hour TTL for active bills, indefinite for terminal
- **`il_document_summaries`** — `url_hash → summary`; IL bill PDF summaries at 5th-grade level
- **`plain_language_titles`** — `record_number → plain_title`; Claude translations cached across restarts
- **`attachment_summaries`** — `url_hash (md5) → summary`; PDF summaries at 5th-grade level
- **`meeting_summaries`** — `meeting_id → summary`; ≤50-word meeting summaries. For no-item meetings, generated from the Agenda PDF (overwrites the generic placeholder text if present).
- **`matter_detail_cache`** — `record_number → {status, data JSONB}`; full `enrich_matter()` output; 1h TTL for active matters, indefinite for settled; primary source for all matter page loads. `legislativeTracker` steps now include `actionByName` (the body that took the action, e.g. "Committee on Finance"). Backfill: `python backfill_action_descriptions.py`
- **`meeting_items`** — `(meeting_id, record_number)` → item metadata (title, type, action, is_routine, order); written by scheduler when fetching agenda content; read by `meeting_matters` route before calling ELMS. Meetings with no items → `hasNoMatters: true` response with `meetingDocuments[]` from meeting `files[]`. For agenda-only meetings, `link_agenda_matters()` populates this table from matter IDs extracted from the Agenda PDF, with `action_name = "discussed in committee"`.
- **`meeting_subscriptions`** — `email + body + confirmed + confirm_token + unsub_token`; meeting email subscribers
- **`matter_subscriptions`** — `email + record_number + last_status + confirmed + tokens`; per-legislation trackers
- **`meeting_email_log`** — `(email, meeting_id, email_type)` UNIQUE; per-subscriber deduplication guard
- **`known_meetings`** — per-meeting state: `meeting_datetime` (full ISO timestamp for DateTrigger scheduling), `elms_status`, `nonroutine_count`, `agenda_sent_at`, `summary_sent_at`, `new_meeting_sent_at`; scheduler reads this instead of polling ELMS on every check

---

## Data Flow — Meetings, Legislation & Matter Loads

All user-facing page loads are designed to be **DB-only**. ELMS and Claude are called only by the background scheduler, which pre-populates every table before users arrive. ELMS fallbacks exist in every route but should almost never fire in steady state.

### Scheduler: how the DB gets populated

Two APScheduler jobs run in the background:

```
sync_meeting_schedule()           runs at startup + every 24 h
────────────────────────────────────────────────────────────────────
  ELMS /meeting-agenda (lightweight list, no agenda content)
       │
       ├─ Upsert each meeting into known_meetings
       │    (meeting_id, body, date, datetime, elms_status)
       │
       ├─ Register DateTrigger one-shot polls at:
       │    • meeting start time   → check_and_send_meeting_emails()
       │    • meeting start + 3h  → check_and_send_meeting_emails()
       │
       └─ NEW meetings only: if subscribers exist, send "new meeting" alert


check_and_send_meeting_emails()   called by DateTrigger + 4 h safety net
────────────────────────────────────────────────────────────────────
  Query known_meetings for meetings needing agenda or summary email
  (never fetches ELMS schedule — sync_meeting_schedule() owns that)
       │
       ├─ For each meeting needing an AGENDA email (upcoming, items exist):
       │    ELMS /meeting-agenda/<id>
       │         │
       │         ├─ Write items → meeting_items
       │         │    (record_number, matter_id, title, type, is_routine, order)
       │         │
       │         ├─ _prewarm_matter_cache() — parallel, max 8 workers
       │         │    for each non-routine item:
       │         │      get_enriched_matter(record_number)
       │         │        ├─ check matter_detail_cache (skip if fresh)
       │         │        ├─ ELMS /matter/recordNumber/<rn>
       │         │        ├─ enrich_matter() (status context, type desc,
       │         │        │   PDF summaries → attachment_summaries)
       │         │        ├─ plain_language_titles() → plain_language_titles
       │         │        └─ write → matter_detail_cache
       │         │
       │         └─ Send agenda email to subscribers
       │
       └─ For each meeting needing a SUMMARY email (past, ≥3 h elapsed):
            same ELMS + prewarm steps above, plus:
            meeting_summary() → Claude Haiku → write → meeting_summaries
            Send summary email to subscribers
```

### User-facing page loads (steady state: all DB)

```
MEETINGS TAB OPEN
─────────────────
Browser → GET /meetings/recent
  └─ SELECT meeting_summaries + known_meetings (LEFT JOIN)
       ORDER BY meeting_date DESC LIMIT 5
       ← { meetings: [...] }                       ← DB only ✓

"Browse all meetings"
─────────────────────
Browser → GET /meetings/all
  └─ same query, LIMIT 50                          ← DB only ✓

CLICK A MEETING
───────────────
Browser → GET /meetings/<id>/matters
  ├─ SELECT meeting_items WHERE meeting_id = <id>  ← DB only ✓
  │    (fallback: ELMS /meeting-agenda/<id> if not yet cached)
  │
  ├─ Parallel: SELECT matter_detail_cache
  │    WHERE record_number IN (page of items)      ← DB only ✓
  │    (fallback: ELMS /matter/recordNumber/<rn> on cache miss)
  │
  └─ plain_language_titles() batch DB lookup       ← DB only ✓
       (fallback: Claude Haiku if title not cached)


LEGISLATION TAB OPEN
─────────────────────
Browser → GET /legislation/recent
  └─ SELECT matter_detail_cache
       WHERE cached_at > NOW() - 60 days
       ORDER BY (data->>'introductionDate') DESC
       filter boilerplate in Python, return top 12  ← DB only ✓
       (fallback: ELMS /search?orderby=introductionDate if cache empty)

LEGISLATION SEARCH
──────────────────
Browser → GET /legislation/search?q=<query>
  ├─ ELMS /search?search=<q>&top=25              ← ELMS (unavoidable for FTS)
  ├─ filter boilerplate
  ├─ Claude Haiku rerank → top 10 record numbers
  └─ SELECT matter_detail_cache
       WHERE record_number IN (top 10 rns)        ← DB only ✓
       (fallback: ELMS search result fields if rn not in cache)

CLICK A MATTER
──────────────
Browser → GET /legislation/matters/<rn>
  └─ get_enriched_matter(rn)
       ├─ SELECT matter_detail_cache WHERE record_number = <rn>
       │    return immediately if cached + not stale  ← DB only ✓
       └─ fallback (rare, new legislation only):
            ELMS /matter/recordNumber/<rn>
            enrich_matter() + plain_language_titles()
            write → matter_detail_cache
```

### Cache staleness policy (`matter_detail_cache`)

| Matter state | TTL |
|---|---|
| Active (in committee, referred) | 1 hour |
| Settled (passed, failed, withdrawn, tabled) | indefinite |

The scheduler re-warms active matters every time a meeting they appear in is processed, so the 1-hour TTL only matters for direct user lookups between scheduler runs.

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
- Meeting `files[]` — meeting-level documents surfaced only for meetings with no agenda items (see below)

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

1. **Sort actions chronologically** — `matter["actions"]` sorted ascending by `actionDate` (ISO strings sort lexicographically)
2. **Status context** — sets `matter["statusContext"]` from `_STATUS_CONTEXT` dict based on `status`/`subStatus`:
   - `in_committee_active` / `in_committee_stale` (>180 days) / `held_in_committee` / `referred` / `passed` / `failed` / `withdrawn` / `tabled`
3. **Action-level context** — referral actions get `action["statusContext"]` = Rule 41 explanation
4. **Type description** — `matter["typeDescription"]` from `_LEGISLATION_TYPES` dict (ordinance, resolution, order, report, etc.)
5. **Direct attachments** — `matter["matterAttachments"]` = `matter["attachments"]` from ELMS (legislation PDFs, etc.). For `.pdf` URLs, `_pdf_summary()` is called to add a `summary` field — cached in `attachment_summaries` table.
6. **Committee chair** — fuzzy match `matter.controllingBody` against `_COMMITTEE_CHAIRS` dict; sets `matter["committeeChair"]`
7. **What can you do?** — `matter["whatCanYouDo"]` list: contact alderperson link + committee chair contact if in committee
8. **Plain language title** — `matter["plainLanguageTitle"]` via `_plain_language_titles()`
9. **Legislative tracker** — `matter["legislativeTracker"]` — list of 5 step dicts built by `_build_legislative_tracker()`:
   - Steps: `referred` → `committee_hearing` → `committee_outcome` → `council_vote` → `mayor_action`
   - Each step: `{id, label, sublabel, status, date, actionName}`
   - Status values: `complete` / `current` (pulsing) / `pending` / `blocked` (orange, for tabled/withdrawn/vetoed/failed) / `not_applicable` (resolutions, orders, etc.)
   - Action name matching: `"Referred"` → step 1; `"Recommended to Pass"` / `"Substituted"` / `"Held in Committee"` etc. → steps 2–3; `"Passed"` / `"Adopted"` / `"Approved"` by City Council → step 4; `"Signed by Mayor"` / `"Vetoed"` → step 5
   - Displayed in frontend as a horizontal stepper (vertical on mobile ≤600px) above the detailed timeline

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
| `SOCRATA_APP_TOKEN` | Optional | Raises Chicago Socrata rate limit from 1 req/s → 10 req/s |
| `LEGISCAN_API_KEY` | Optional | Legiscan IL legislation API key (default key in config.py) |

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
