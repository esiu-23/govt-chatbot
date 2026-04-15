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
    feedback_note TEXT
);

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
