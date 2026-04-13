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
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
import gc
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
import faiss
import numpy as np
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from fastembed import TextEmbedding
import anthropic
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
VECTORS_DIR = Path("vectors")
MODEL_NAME  = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
TOP_K       = 5

index_path    = VECTORS_DIR / "index.faiss"
metadata_path = VECTORS_DIR / "metadata.json"

# ---------------------------------------------------------------------------
# Assets — loaded in each worker via gunicorn's post_fork hook (see
# gunicorn.conf.py) so ONNX threads are never inherited across fork().
# Call load_resources() directly when running outside gunicorn (e.g. locally).
# ---------------------------------------------------------------------------
_embedder:    "TextEmbedding | None" = None
_faiss_index: "faiss.Index | None"  = None
SCRAPE_DATE:  str = ""
TOTAL_CHUNKS: int = 0
chunks:       list = []


def load_resources() -> None:
    global _embedder, _faiss_index, SCRAPE_DATE, TOTAL_CHUNKS, chunks

    if not index_path.exists() or not metadata_path.exists():
        raise FileNotFoundError(
            "Vector index not found. Run `python scrape_and_index.py` first."
        )

    print("Loading embedding model...")
    _local_cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fastembed_cache")
    _cache_dir   = os.environ.get("FASTEMBED_CACHE_DIR", _local_cache)
    try:
        _embedder = TextEmbedding(MODEL_NAME, threads=1, cache_dir=_cache_dir)
    except PermissionError:
        print(f"Warning: could not write to {_cache_dir}, falling back to {_local_cache}")
        _embedder = TextEmbedding(MODEL_NAME, threads=1, cache_dir=_local_cache)

    print("Loading FAISS index...")
    _faiss_index = faiss.read_index(str(index_path))

    with open(metadata_path, encoding="utf-8") as f:
        _meta = json.load(f)

    SCRAPE_DATE  = _meta["scrape_date"]
    TOTAL_CHUNKS = _meta["total_chunks"]
    chunks       = _meta["chunks"]

    gc.collect()
    print(f"Ready — {TOTAL_CHUNKS} chunks indexed from scrape on {SCRAPE_DATE}\n")

# ---------------------------------------------------------------------------
# Session log — SQLite locally, Turso in production
# Set TURSO_DATABASE_URL + TURSO_AUTH_TOKEN env vars to enable Turso.
# ---------------------------------------------------------------------------
DB_PATH = Path(os.environ.get("DB_PATH", "conversations.db"))

_TURSO_URL   = os.environ.get("TURSO_DATABASE_URL")
_TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
_USE_TURSO   = bool(_TURSO_URL and _TURSO_TOKEN)

if _USE_TURSO:
    import glob as _glob
    import libsql_experimental as libsql
    # Render's filesystem is ephemeral: the replica db file is wiped on each
    # deploy but libsql's metadata files can survive, leaving the replica in a
    # corrupt state ("metadata file exists but db file does not").
    # Glob catches replica.db, replica.db-wal, replica.db-shm, and any other
    # internal files libsql creates with that prefix.
    for _stale in _glob.glob("replica.db*"):
        Path(_stale).unlink(missing_ok=True)


@contextmanager
def _db():
    """Context manager that yields an open DB connection and handles commit/sync/close."""
    if _USE_TURSO:
        con = libsql.connect("replica.db", sync_url=_TURSO_URL, auth_token=_TURSO_TOKEN)
        con.sync()
        try:
            yield con
            con.commit()
            con.sync()
        finally:
            con.close()
    else:
        con = sqlite3.connect(DB_PATH)
        try:
            yield con
            con.commit()
        finally:
            con.close()


def _init_db():
    with _db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id    TEXT PRIMARY KEY,
                started_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_updated  DATETIME DEFAULT CURRENT_TIMESTAMP,
                lang          TEXT,
                conversation  TEXT NOT NULL DEFAULT '[]',
                feedback      TEXT,
                feedback_note TEXT
            )
        """)


_init_db()


def upsert_turn(session_id, lang, user_turn, assistant_turn):
    """Append a user + assistant turn pair to this session's conversation JSON."""
    if not session_id:
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with _db() as con:
            con.execute(
                "INSERT OR IGNORE INTO sessions (session_id, lang, conversation) VALUES (?, ?, '[]')",
                (session_id, lang),
            )
            row = con.execute(
                "SELECT conversation FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            turns = json.loads(row[0]) if row else []
            turns.append({**user_turn,      "timestamp": ts})
            turns.append({**assistant_turn, "timestamp": ts})
            con.execute(
                "UPDATE sessions SET conversation = ?, last_updated = CURRENT_TIMESTAMP, lang = ? "
                "WHERE session_id = ?",
                (json.dumps(turns, ensure_ascii=False), lang, session_id),
            )
    except Exception as exc:
        app.logger.error("DB upsert_turn failed: %s", exc)


def _init_debug_log():
    with _db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS source_debug_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                session_id     TEXT,
                question       TEXT,
                retrieved_urls TEXT,
                used_urls      TEXT,
                filtered_urls  TEXT,
                fallback_used  INTEGER
            )
        """)


_init_debug_log()


def log_source_debug(session_id, question, retrieved_urls, used_urls, filtered_urls, fallback_used):
    try:
        with _db() as con:
            con.execute(
                "INSERT INTO source_debug_log "
                "(session_id, question, retrieved_urls, used_urls, filtered_urls, fallback_used) "
                "VALUES (?, ?, ?, ?, ?, ?)",
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
    """Write the session-level rating and optional note."""
    if not session_id:
        return
    try:
        with _db() as con:
            con.execute(
                "UPDATE sessions SET feedback = ?, feedback_note = ? WHERE session_id = ?",
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
    data       = request.get_json(silent=True) or {}
    question   = data.get("question", "").strip()
    lang       = data.get("lang", "en").strip() or "en"
    history    = data.get("history", [])
    session_id = data.get("session_id", "")[:64]

    if not question:
        return jsonify({"error": "No question provided"}), 400

    LANG_NAMES = {
        "en": "English", "es": "Spanish", "pl": "Polish",
        "zh": "Chinese (Simplified)", "ar": "Arabic",
        "tl": "Tagalog", "hi": "Hindi",
    }
    lang_name = LANG_NAMES.get(lang, "English")

    # 1. Embed the question
    print(f"[chat] embedding question: {question[:50]!r}")
    q_embedding = np.array(list(_embedder.embed([question])))
    print("[chat] embedding done")

    # 2. Cosine similarity search via FAISS
    print("[chat] searching FAISS")
    scores, indices = _faiss_index.search(q_embedding.astype(np.float32), TOP_K)
    print("[chat] FAISS done")

    # 3. Collect retrieved chunks + deduplicated sources
    context_parts = []
    sources       = []
    seen_urls     = set()

    SCORE_THRESHOLD = 0.35  # cosine similarity floor; drop clearly irrelevant chunks
    for score, idx in zip(scores[0], indices[0]):
        if score < SCORE_THRESHOLD:
            continue
        chunk = chunks[idx]
        context_parts.append(f"[Source: {chunk['title']}]\n{chunk['text']}")
        if chunk["url"] not in seen_urls:
            seen_urls.add(chunk["url"])
            sources.append({"title": chunk["title"], "url": chunk["url"]})

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

    print("[chat] calling Anthropic API")
    try:
        message = client.messages.create(
            model      = "claude-haiku-4-5-20251001",
            max_tokens = 200,
            system     = SYSTEM_PROMPT,
            messages   = messages,
        )
        print("[chat] Anthropic API done")
    except anthropic.BadRequestError as exc:
        # Context window exceeded (prompt too long)
        app.logger.warning("Context window exceeded: %s", exc)
        return jsonify({
            "type"   : "limit",
            "answer" : (
                "This tool is still being built and can only remember so much of a conversation. "
                "Want to see it improve? Tap \U0001f44d below! "
            ),
            "sources": [],
        })

    # Truncated response — model hit max_tokens mid-generation
    if message.stop_reason == "max_tokens":
        app.logger.warning("Response truncated (max_tokens) for question: %s", question)
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
        # Try exact URL match first; fall back to domain-level match (e.g. "chicagoparkdistrict.com")
        filtered_sources = [s for s in sources if s["url"] in used_urls]
        if not filtered_sources:
            filtered_sources = [
                s for s in sources
                if any(u in s["url"] for u in used_urls)
            ]
    else:
        filtered_sources = []

    # Last-resort fallback: if Claude cited nothing (or nothing matched),
    # surface the raw FAISS-retrieved sources so the user still gets links.
    if not filtered_sources and sources:
        filtered_sources = sources
        fallback_used = True

    print("[chat] logging to DB")
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
    print("[chat] done")
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
    app.run(debug=False, host="0.0.0.0", port=port, use_reloader=False)
