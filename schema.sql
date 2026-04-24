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
