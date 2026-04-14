"""
api.py
------
Flask backend for the Chicago Gov RAG chatbot.

Endpoints:
  GET  /          → serves static/index.html
  POST /chat      → { "question": "..." } → { "answer", "sources", "scrape_date", "disclaimer" }
  GET  /health    → { "status": "ok", "scrape_date": "...", "total_chunks": N }
  POST /feedback  → { "type": "up"|"down", "note": "...", "session_id": "..." }

Start:  python api.py
"""

import os
import gc
import json
import time
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
import numpy as np
import psycopg2
import psycopg2.extras
import psycopg2.pool
import voyageai
from pgvector.psycopg2 import register_vector
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
import anthropic
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MODEL_NAME    = "voyage-multilingual-2"
TOP_K         = 5
SCORE_THRESHOLD = 0.35   # minimum cosine similarity; drops irrelevant chunks
DATABASE_URL  = os.environ.get("DATABASE_URL")


def _ipv4_dsn(dsn: str) -> str:
    """Inject hostaddr= (IPv4) into the DSN so psycopg2 never tries IPv6."""
    import urllib.parse as _up
    # Parse as a URL to extract the hostname
    parsed = _up.urlparse(dsn)
    hostname = parsed.hostname
    if not hostname:
        return dsn
    try:
        # getaddrinfo with AF_INET forces IPv4 resolution
        infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
        ipv4 = infos[0][4][0]
    except Exception:
        return dsn  # can't resolve; let psycopg2 try normally
    # Append hostaddr to the DSN query string so psycopg2 uses it
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}hostaddr={ipv4}"

# ---------------------------------------------------------------------------
# Globals set once in load_resources()
# ---------------------------------------------------------------------------
_voyage:      "voyageai.Client | None" = None
_pool:        "psycopg2.pool.ThreadedConnectionPool | None" = None
SCRAPE_DATE:  str = ""
TOTAL_CHUNKS: int = 0


# ---------------------------------------------------------------------------
# Database — pooled connections, auto-commit/rollback/return
# ---------------------------------------------------------------------------
@contextmanager
def _db():
    conn = _pool.getconn()
    register_vector(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


# ---------------------------------------------------------------------------
# Startup — called once per worker before serving requests
# ---------------------------------------------------------------------------
def load_resources() -> None:
    global _voyage, _pool, SCRAPE_DATE, TOTAL_CHUNKS

    print("Initialising Voyage AI client...", flush=True)
    _voyage = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"], timeout=30)

    print("Connecting to Supabase...", flush=True)
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2, maxconn=10, dsn=_ipv4_dsn(DATABASE_URL), connect_timeout=10
    )

    with _db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT scrape_date, total_chunks FROM scrape_info ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("No scrape data found. Run `python scrape_and_index.py` first.")
        SCRAPE_DATE, TOTAL_CHUNKS = row

    gc.collect()
    print(f"Ready — {TOTAL_CHUNKS} chunks in Supabase, scraped {SCRAPE_DATE}\n")


# ---------------------------------------------------------------------------
# Conversation logging
# ---------------------------------------------------------------------------
def upsert_turn(session_id, lang, user_turn, assistant_turn):
    """Append a user + assistant turn pair to this session's conversation JSON."""
    if not session_id:
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sessions (session_id, lang, conversation) VALUES (%s, %s, '[]'::jsonb) "
                "ON CONFLICT (session_id) DO NOTHING",
                (session_id, lang),
            )
            cur.execute(
                "SELECT conversation FROM sessions WHERE session_id = %s", (session_id,)
            )
            row = cur.fetchone()
            turns = row[0] if row else []
            turns.append({**user_turn,      "timestamp": ts})
            turns.append({**assistant_turn, "timestamp": ts})
            cur.execute(
                "UPDATE sessions SET conversation = %s::jsonb, last_updated = NOW(), lang = %s "
                "WHERE session_id = %s",
                (json.dumps(turns, ensure_ascii=False), lang, session_id),
            )
    except Exception as exc:
        app.logger.error("DB upsert_turn failed: %s", exc)


def log_source_debug(session_id, question, retrieved_urls, used_urls, filtered_urls, fallback_used):
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO source_debug_log "
                "(session_id, question, retrieved_urls, used_urls, filtered_urls, fallback_used) "
                "VALUES (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s)",
                (
                    session_id,
                    question,
                    json.dumps(retrieved_urls),
                    json.dumps(sorted(used_urls)),
                    json.dumps(filtered_urls),
                    int(fallback_used),
                ),
            )
    except Exception as exc:
        app.logger.error("DB log_source_debug failed: %s", exc)


def log_feedback(session_id, feedback_type, note):
    if not session_id:
        return
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE sessions SET feedback = %s, feedback_note = %s WHERE session_id = %s",
                (feedback_type, note or None, session_id),
            )
    except Exception as exc:
        app.logger.error("DB feedback update failed: %s", exc)


# ---------------------------------------------------------------------------
# Claude client
# ---------------------------------------------------------------------------
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=30.0)

SYSTEM_PROMPT = (
    "You are a concise assistant for City of Chicago government services.\n\n"

    "HARD RULE: Every response must be 50 words or fewer (not counting the SOURCES line). No exceptions.\n\n"

    "Chicago services are organized in three levels:\n"
    "  Level 1 — top category: Public Safety | Business & Licensing | "
    "Housing & Buildings | Health & Human Services | "
    "Transportation & Infrastructure | Finance & Administration | "
    "Culture, Arts & Recreation | City Government | City Services\n"
    "  Level 2 — individual department within a Level 1 category\n"
    "  Level 3 — specific service, program, contact info, or how-to steps\n\n"

    "CONVERSATION HISTORY: Always read prior turns before deciding whether to "
    "clarify. A short reply like 'Help applying', 'the former', 'the first "
    "one', or 'yes' is a follow-up — resolve its meaning from the prior "
    "exchange before asking another question.\n\n"

    "CLARIFYING QUESTIONS: Only ask if the topic is still ambiguous AFTER "
    "reading the full conversation history. If the user's question does not "
    "clearly indicate which Level 1 category, which department (Level 2), or "
    "which type of information (Level 3) they need, respond ONLY with:\n"
    "  CLARIFY: <one short question that narrows down what they need>\n"
    "Do not answer and clarify at the same time. Choose one.\n\n"
    "CRITICAL: If the user's current message is a direct answer to your previous "
    "clarifying question — even if it is short (e.g. 'a list', 'the first one', "
    "'yes') — do NOT ask another clarifying question. Use their answer to resolve "
    "ambiguity and provide a real answer immediately.\n\n"

    "Otherwise answer using ONLY information from the City of Chicago website. "
    "Name the relevant department when helpful. "
    "If the answer is not on the City of Chicago website, say so and suggest chicago.gov or 311.\n\n"

    "HARD RULE — URLS: Never invent, guess, or construct a URL. Only use URLs "
    "that appear verbatim in the City of Chicago website content provided above. "
    "If no URL is present in that content, do not include any link in your response.\n\n"

    "SOURCES LINE: After your answer, on a new line, write exactly:\n"
    "  SOURCES: <comma-separated list of the source URLs you actually used from the context>\n"
    "Always include the most specific source URL, or the top-level source used:\n"
    "chicago.gov for City of Chicago services, chicagoparkdistrict.com for parks information, cps.edu for Chicago Public Schools information.\n"
    "If you used no specific URL from the context, write: SOURCES: none\n"
    "Only list URLs that actually appear verbatim in the context provided."
)

DISCLAIMER_TEMPLATE = (
    "Information sourced from chicago.gov as of {date}. "
    "Content may have changed — visit the sources directly to confirm."
)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static")


@app.route("/")
def index():
    app.logger.info("index pinged")
    return send_from_directory("static", "index.html")


@app.route("/health")
def health():
    return jsonify({
        "status"       : "ok",
        "scrape_date"  : SCRAPE_DATE,
        "total_chunks" : TOTAL_CHUNKS,
        "model"        : MODEL_NAME,
    })


@app.route("/chat", methods=["POST"])
def chat():
    app.logger.info("[chat] handler entered")
    data       = request.get_json(silent=True) or {}
    question   = data.get("question", "").strip()
    lang       = data.get("lang", "en").strip() or "en"
    history    = data.get("history", [])
    session_id = data.get("session_id", "")[:64]

    app.logger.info("[chat] parsed request: question=%r lang=%r session_id=%r history_turns=%d",
                       question[:80], lang, session_id, len(history))

    if not question:
        app.logger.warning("[chat] rejected: empty question")
        return jsonify({"error": "No question provided"}), 400

    t0 = time.monotonic()
    def elapsed():
        return f"{time.monotonic() - t0:.2f}s"

    LANG_NAMES = {
        "en": "English", "es": "Spanish", "pl": "Polish",
        "zh": "Chinese (Simplified)", "ar": "Arabic",
        "tl": "Tagalog", "hi": "Hindi",
    }
    lang_name = LANG_NAMES.get(lang, "English")

    # 1. Embed the question via Voyage AI
    app.logger.info("[chat] +%s embedding question: %r", elapsed(), question[:50])
    result = _voyage.embed([question], model=MODEL_NAME, input_type="query")
    q_vec = np.array(result.embeddings[0], dtype=np.float32)
    app.logger.info("[chat] +%s embedding done", elapsed())

    # 2. Cosine similarity search via pgvector
    app.logger.info("[chat] +%s querying Supabase", elapsed())
    with _db() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            WITH ranked AS (
                SELECT id, url, title, text,
                       embedding <=> %s AS distance
                FROM chunks
                ORDER BY distance
                LIMIT %s
            )
            SELECT id, url, title, text, 1 - distance AS similarity
            FROM ranked
            """,
            (q_vec, TOP_K),
        )
        rows = cur.fetchall()
    app.logger.info("[chat] +%s query done", elapsed())

    # 3. Collect retrieved chunks + deduplicated sources
    context_parts = []
    sources       = []
    seen_urls     = set()

    for row in rows:
        if row["similarity"] < SCORE_THRESHOLD:
            continue
        context_parts.append(f"[Source: {row['title']}]\n{row['text']}")
        if row["url"] not in seen_urls:
            seen_urls.add(row["url"])
            sources.append({"title": row["title"], "url": row["url"]})

    context = "\n\n---\n\n".join(context_parts)

    # 4. Generate answer with Claude Haiku
    clarify_count = int(data.get("clarify_count", 0))

    user_content = (
        f"Respond in {lang_name}.\n\n"
        f"Context from chicago.gov:\n\n{context}\n\n"
        f"Question: {question}"
    )

    if clarify_count >= 1:
        user_content += (
            "\n\nNOTE: You have already asked 1 clarifying question in a row. "
            "Do NOT ask another clarifying question. "
            "Answer with what you can from the context above, or say "
            "'I don't know' and suggest the most relevant links from the context."
        )

    messages = []
    for turn in history:
        role    = turn.get("role", "")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_content})

    app.logger.info("[chat] +%s calling Anthropic API", elapsed())
    try:
        message = client.messages.create(
            model      = "claude-haiku-4-5-20251001",
            max_tokens = 200,
            system     = SYSTEM_PROMPT,
            messages   = messages,
        )
        app.logger.info("[chat] +%s Anthropic API done", elapsed())
    except anthropic.BadRequestError as exc:
        app.logger.info("Context window exceeded: %s", exc)
        return jsonify({
            "type"   : "limit",
            "answer" : (
                "This tool is still being built and can only remember so much of a conversation. "
                "Want to see it improve? Tap \U0001f44d below! "
            ),
            "sources": [],
        })

    if message.stop_reason == "max_tokens":
        app.logger.info("Response truncated (max_tokens) for question: %s", question)
        return jsonify({
            "type"   : "limit",
            "answer" : (
                "This tool is still being built and ran into a limit. "
                "Want to see it get better? Tap \U0001f44d below! "
            ),
            "sources": [],
        })

    raw = message.content[0].text.strip()

    # Detect clarifying question
    if clarify_count < 2 and raw.upper().startswith("CLARIFY:"):
        clarification = raw[len("CLARIFY:"):].strip()
        upsert_turn(session_id, lang,
            {"role": "user",      "content": question},
            {"role": "assistant", "content": clarification,
             "type": "clarification", "sources": []},
        )
        return jsonify({
            "type"   : "clarification",
            "answer" : clarification,
            "sources": [],
        })

    # Parse SOURCES: line Claude emits at the end
    answer_text = raw
    used_urls: set[str] = set()
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("SOURCES:"):
            payload = line.split(":", 1)[1].strip()
            if payload.lower() != "none":
                for part in payload.split(","):
                    url = part.strip().rstrip(".")
                    if url:
                        used_urls.add(url)
            answer_text = "\n".join(lines[:i]).strip()
            break

    fallback_used = False
    if used_urls:
        filtered_sources = [s for s in sources if s["url"] in used_urls]
        if not filtered_sources:
            filtered_sources = [
                s for s in sources
                if any(u in s["url"] for u in used_urls)
            ]
    else:
        filtered_sources = []

    if not filtered_sources and sources:
        filtered_sources = sources
        fallback_used = True

    app.logger.info("[chat] +%s logging to DB", elapsed())
    log_source_debug(
        session_id,
        question,
        retrieved_urls=[s["url"] for s in sources],
        used_urls=used_urls,
        filtered_urls=[s["url"] for s in filtered_sources],
        fallback_used=fallback_used,
    )
    upsert_turn(session_id, lang,
        {"role": "user",      "content": question},
        {"role": "assistant", "content": answer_text, "type": "answer",
         "sources": [s["url"] for s in filtered_sources],
         "used_urls": sorted(used_urls),
         "fallback_used": fallback_used},
    )
    app.logger.warning("[chat] +%s done", elapsed())

    return jsonify({
        "type"       : "answer",
        "answer"     : answer_text,
        "sources"    : filtered_sources,
        "scrape_date": SCRAPE_DATE,
        "disclaimer" : DISCLAIMER_TEMPLATE.format(date=SCRAPE_DATE),
    })


@app.route("/feedback", methods=["POST"])
def feedback():
    data          = request.get_json(silent=True) or {}
    feedback_type = data.get("type", "").strip()
    note          = data.get("note", "").strip()
    session_id    = data.get("session_id", "")[:64]

    if feedback_type not in ("up", "down"):
        return jsonify({"ok": False, "error": "Invalid feedback type."}), 400

    log_feedback(session_id, feedback_type, note)
    return jsonify({"ok": True})


if __name__ == "__main__":
    load_resources()
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=False, host="0.0.0.0", port=port, use_reloader=False, threaded=True)
