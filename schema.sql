-- Run this once in the Supabase SQL editor before starting the app.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Vector store
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scrape_info (
    id           SERIAL PRIMARY KEY,
    scrape_date  TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    total_pages  INTEGER NOT NULL,
    total_chunks INTEGER NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chunks (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL,
    text        TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    level1      TEXT,
    level2      TEXT,
    level3      TEXT,
    embedding   vector(1024) NOT NULL
);

-- HNSW index — makes cosine similarity queries fast
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- Conversation log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    started_at    TIMESTAMPTZ DEFAULT NOW(),
    last_updated  TIMESTAMPTZ DEFAULT NOW(),
    lang          TEXT,
    conversation  JSONB NOT NULL DEFAULT '[]',
    feedback      TEXT,
    feedback_note TEXT,
    last_intent   JSONB         -- last known data-query intent for this session
);

-- Migration for existing deployments:
-- ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_intent JSONB;

CREATE TABLE IF NOT EXISTS source_debug_log (
    id             SERIAL PRIMARY KEY,
    logged_at      TIMESTAMPTZ DEFAULT NOW(),
    session_id     TEXT,
    question       TEXT,
    retrieved_urls JSONB,
    used_urls      JSONB,
    filtered_urls  JSONB,
    fallback_used  INTEGER
);

CREATE TABLE IF NOT EXISTS data_query_log (
    id               SERIAL PRIMARY KEY,
    logged_at        TIMESTAMPTZ DEFAULT NOW(),
    session_id       TEXT,
    question         TEXT,
    dataset          TEXT,
    where_clause     TEXT,
    select_clause    TEXT,
    records_returned INTEGER,
    raw_result       JSONB
);

-- ---------------------------------------------------------------------------
-- Legislation AI cache
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS plain_language_titles (
    record_number  TEXT PRIMARY KEY,
    original_title TEXT,          -- original ELMS title text (for auditing / text-based lookup)
    plain_title    TEXT NOT NULL,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS attachment_summaries (
    url_hash   TEXT PRIMARY KEY,  -- md5(url)
    url        TEXT NOT NULL,
    file_name  TEXT,              -- original fileName from ELMS attachment
    summary    TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS meeting_summaries (
    meeting_id   TEXT PRIMARY KEY,
    body         TEXT,            -- committee / body name
    meeting_date TEXT,            -- ISO date (YYYY-MM-DD)
    summary      TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Migration for existing deployments:
-- ALTER TABLE plain_language_titles ADD COLUMN IF NOT EXISTS original_title TEXT;
-- ALTER TABLE attachment_summaries  ADD COLUMN IF NOT EXISTS file_name TEXT;
-- ALTER TABLE meeting_summaries     ADD COLUMN IF NOT EXISTS body TEXT;
-- ALTER TABLE meeting_summaries     ADD COLUMN IF NOT EXISTS meeting_date TEXT;

-- ---------------------------------------------------------------------------
-- Matter detail cache (avoids re-calling ELMS on repeat page views)
-- Invalidate by deleting the row when matter_tracking poll detects a status change.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS matter_detail_cache (
    record_number TEXT PRIMARY KEY,
    cached_at     TIMESTAMPTZ DEFAULT NOW(),
    status        TEXT,
    data          JSONB NOT NULL
);

-- Migration for existing deployments:
-- (new table — just run the CREATE above)

-- ---------------------------------------------------------------------------
-- Email subscriptions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meeting_subscriptions (
    id             SERIAL PRIMARY KEY,
    email          TEXT NOT NULL,
    body           TEXT NOT NULL,           -- e.g. "City Council", "Committee on Finance"
    confirmed      BOOLEAN DEFAULT FALSE,
    confirm_token  TEXT UNIQUE NOT NULL,
    unsub_token    TEXT UNIQUE NOT NULL,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(email, body)
);
CREATE INDEX IF NOT EXISTS meeting_subscriptions_body_idx ON meeting_subscriptions(body);

-- Migration for existing deployments:
-- ALTER TABLE meeting_subscriptions ADD CONSTRAINT meeting_subscriptions_email_body_key UNIQUE (email, body);

CREATE TABLE IF NOT EXISTS matter_subscriptions (
    id              SERIAL PRIMARY KEY,
    email           TEXT NOT NULL,
    record_number   TEXT NOT NULL,
    last_status     TEXT,                   -- snapshot used to detect status changes
    confirmed       BOOLEAN DEFAULT FALSE,
    confirm_token   TEXT UNIQUE NOT NULL,
    unsub_token     TEXT UNIQUE NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(email, record_number)
);

CREATE TABLE IF NOT EXISTS meeting_email_log (
    id          SERIAL PRIMARY KEY,
    email       TEXT NOT NULL,
    meeting_id  TEXT NOT NULL,
    email_type  TEXT NOT NULL,              -- 'agenda' | 'summary'
    sent_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(email, meeting_id, email_type)
);

-- Tracks each known meeting's state so the scheduler can diff across polls
-- instead of re-examining everything from scratch each run.
CREATE TABLE IF NOT EXISTS known_meetings (
    meeting_id        TEXT PRIMARY KEY,
    body              TEXT NOT NULL,
    meeting_date      TEXT NOT NULL,        -- YYYY-MM-DD
    elms_status       TEXT,                 -- raw status from ELMS ("Scheduled", "Published", "Completed", etc.)
    nonroutine_count  INTEGER DEFAULT 0,    -- non-routine item count at last check; 0 = agenda not yet seen
    routine_count     INTEGER DEFAULT 0,    -- routine item count (claims, permits, etc.)
    location          TEXT,                 -- meeting room/address from ELMS
    agenda_sent_at    TIMESTAMPTZ,          -- non-NULL once agenda emails have been dispatched
    summary_sent_at   TIMESTAMPTZ,          -- non-NULL once summary emails have been dispatched
    first_seen_at     TIMESTAMPTZ DEFAULT NOW(),
    last_checked_at   TIMESTAMPTZ DEFAULT NOW()
);
-- Migration for existing deployments:
-- ALTER TABLE known_meetings ADD COLUMN IF NOT EXISTS routine_count INTEGER DEFAULT 0;
-- ALTER TABLE known_meetings ADD COLUMN IF NOT EXISTS location TEXT;
CREATE INDEX IF NOT EXISTS known_meetings_date_idx ON known_meetings(meeting_date);
CREATE INDEX IF NOT EXISTS known_meetings_body_idx ON known_meetings(body);

-- ---------------------------------------------------------------------------
-- Illinois state additions
-- ---------------------------------------------------------------------------

-- Tag existing and new chunks by government scope (city vs. state)
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS source_scope TEXT DEFAULT 'city';

-- Tag data queries by scope
ALTER TABLE data_query_log ADD COLUMN IF NOT EXISTS query_scope TEXT DEFAULT 'city';

-- Illinois Legiscan bill plain-language title cache (mirrors plain_language_titles)
CREATE TABLE IF NOT EXISTS il_plain_language_titles (
    bill_id        TEXT PRIMARY KEY,   -- Legiscan bill_id (integer stored as text)
    session_id     TEXT,               -- Legiscan session_id for reference
    original_title TEXT,
    plain_title    TEXT NOT NULL,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Illinois Legiscan bill detail cache (mirrors matter_detail_cache)
CREATE TABLE IF NOT EXISTS il_bill_detail_cache (
    bill_id    TEXT PRIMARY KEY,
    cached_at  TIMESTAMPTZ DEFAULT NOW(),
    status     TEXT,
    data       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS il_bill_detail_cache_cached_at_idx
    ON il_bill_detail_cache(cached_at);

-- Illinois Legiscan document/amendment summary cache (mirrors attachment_summaries)
CREATE TABLE IF NOT EXISTS il_document_summaries (
    url_hash   TEXT PRIMARY KEY,  -- md5(url)
    url        TEXT NOT NULL,
    doc_type   TEXT,              -- 'bill_text' | 'amendment' | 'supplement'
    summary    TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
