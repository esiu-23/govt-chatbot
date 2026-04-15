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
import re
import csv
import json
import time
import socket
import ssl
import logging
import certifi
import urllib.request
import urllib.parse
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

# ---------------------------------------------------------------------------
# Chicago Data Portal (Socrata SODA API) — no auth required for reads
# ---------------------------------------------------------------------------
SOCRATA_BASE = "https://data.cityofchicago.org/resource"

DATASETS = {
    "business_licenses": "r5kz-chrr",
    "building_permits":  "ydr8-5enu",
    "crime":             "ijzp-q8t2",
    "311_requests":      "v6vf-nfxy",
}

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
SOCRATA_APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN", "")

# ---------------------------------------------------------------------------
# Community area lookup — loaded at startup from Boundaries CSV
# Keys are lowercase for case-insensitive matching.
# ---------------------------------------------------------------------------
COMMUNITY_AREA_BY_NAME: dict[str, int] = {}   # "west loop" → 28
COMMUNITY_AREA_BY_NUM:  dict[int, str] = {}   # 28 → "West Loop"

_HERE = Path(__file__).parent


def _load_community_areas() -> None:
    """Populate COMMUNITY_AREA_BY_NAME / _BY_NUM from the Boundaries CSV."""
    global COMMUNITY_AREA_BY_NAME, COMMUNITY_AREA_BY_NUM
    matches = sorted(_HERE.glob("Boundaries_-_Community_Areas*.csv"))
    if not matches:
        print("WARNING: No community areas CSV found — neighborhood name lookup disabled.", flush=True)
        return
    csv_path = matches[-1]          # use the most recent dated file if multiple exist
    print(f"Loading community areas from {csv_path.name}...", flush=True)
    by_name: dict[str, int] = {}
    by_num:  dict[int, str] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            num  = int(row["AREA_NUMBE"])
            name = row["COMMUNITY"].strip().title()   # "WEST LOOP" → "West Loop"
            by_name[name.lower()] = num
            by_num[num] = name
    COMMUNITY_AREA_BY_NAME = by_name
    COMMUNITY_AREA_BY_NUM  = by_num
    print(f"  Loaded {len(by_num)} community areas.", flush=True)


def _translate_community_areas_in_where(where: str) -> str:
    """Replace any quoted community area name in a SODA $where clause with its numeric code.

    Handles single- and double-quoted strings, case-insensitively.
    E.g.  community_area = 'West Loop'  →  community_area = 28
    """
    for name_lower, num in COMMUNITY_AREA_BY_NAME.items():
        for q in ("'", '"'):
            # Match the quoted name (any casing) and replace with the bare integer
            pattern = re.compile(re.escape(q) + re.escape(name_lower) + re.escape(q), re.IGNORECASE)
            where = pattern.sub(str(num), where)
    return where

# ---------------------------------------------------------------------------
# Pre-routing: structured intent parsing via Claude
# ---------------------------------------------------------------------------

# Whether each dataset supports community-area location filtering
DATASET_HAS_LOCATION: dict[str, bool] = {
    "crime": True,
    "building_permits": True,
    "business_licenses": True,
    "311_requests": True,
}

DATASET_LABELS: dict[str, str] = {
    "crime": "crime incidents",
    "building_permits": "building permits",
    "business_licenses": "business licenses",
    "311_requests": "311 service requests",
}

INTENT_TOOL = {
    "name": "parse_data_query_intent",
    "description": (
        "Extract structured intent from a user question. "
        "Identify whether the question asks for quantitative data from one of the four "
        "Chicago Open Data Portal datasets, and extract the time period and location if present."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_data_query": {
                "type": "boolean",
                "description": (
                    "True ONLY if the user is asking a quantitative question "
                    "(how many, count, total, rate, trend, etc.) about one of these four topics: "
                    "crime or arrests, building permits, business licenses, or 311 service requests. "
                    "False for questions about schools, parks, transit, health, or any general info."
                ),
            },
            "dataset": {
                "type": "string",
                "enum": ["crime", "building_permits", "business_licenses", "311_requests"],
                "description": "The matching dataset key. Include only when is_data_query is true.",
            },
            "has_time": {
                "type": "boolean",
                "description": (
                    "True if the question specifies a time period — a year, month, date range, "
                    "or relative phrase like 'last year', 'this month', 'since 2022', etc."
                ),
            },
            "time_phrase": {
                "type": "string",
                "description": "The time period as the user stated it, e.g. '2024', 'last year'. Empty string if none.",
            },
            "has_location": {
                "type": "boolean",
                "description": (
                    "True if the question names a specific Chicago location (neighborhood, "
                    "community area, ward) OR explicitly says 'all of Chicago', 'citywide', "
                    "'the whole city', etc."
                ),
            },
            "location_phrase": {
                "type": "string",
                "description": "The location as the user stated it, e.g. 'Logan Square', 'all of Chicago'. Empty string if none.",
            },
            "is_citywide": {
                "type": "boolean",
                "description": (
                    "True if the user wants data for all of Chicago with no specific neighborhood. "
                    "False if a specific area or neighborhood is named."
                ),
            },
        },
        "required": ["is_data_query", "has_time", "has_location", "is_citywide"],
    },
}

# Words that look like location candidates but are not neighborhood names
_LOCATION_SKIP_WORDS = frozenset({
    "chicago", "illinois", "il", "city", "the", "a", "an",
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
    "2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026", "2027",
    "recent", "last", "this", "past", "current", "total", "all", "any",
})

# Matches "in/near/around/within/at [the] <candidate phrase>"
_LOCATION_PREP_RE = re.compile(
    r"\b(?:in|around|near|within|at)\s+(?:the\s+)?([A-Za-z][A-Za-z\s'\-]{1,35})",
    re.IGNORECASE,
)



_CITYWIDE_RE = re.compile(
    r"\b(all of chicago|citywide|city-wide|whole city|entire city|all chicago|no specific|everywhere)\b",
    re.IGNORECASE,
)


def _check_location_in_query(question: str) -> dict:
    """Detect and validate any neighborhood/location mention in a data query.

    Returns one of:
      {"status": "citywide"}  — user wants all-Chicago data
      {"status": "valid",   "name": "West Loop", "num": 28}
      {"status": "invalid", "mention": "River North"}
      {"status": "none"}   — no location phrase detected
    """
    if _CITYWIDE_RE.search(question):
        return {"status": "citywide"}

    if not COMMUNITY_AREA_BY_NAME:
        return {"status": "none"}

    q_lower = question.lower()

    # 1. Check for a known community area name (longest match first to avoid partial hits)
    for name_lower, num in sorted(COMMUNITY_AREA_BY_NAME.items(), key=lambda x: -len(x[0])):
        if re.search(r"\b" + re.escape(name_lower) + r"\b", q_lower):
            return {"status": "valid", "name": COMMUNITY_AREA_BY_NUM[num], "num": num}

    # 2. Check for a location-preposition phrase that did NOT match a community area
    for m in _LOCATION_PREP_RE.finditer(question):
        candidate = m.group(1).strip()
        # Trim trailing noise ("in 2025", "for the year", etc.)
        candidate = re.sub(
            r"\s*(?:in|for|and|,|\?|during|the year).*$", "", candidate, flags=re.IGNORECASE
        ).strip()
        if not candidate:
            continue
        first_word = candidate.lower().split()[0]
        # Skip temporal words, pure numbers, and the city name itself
        if first_word in _LOCATION_SKIP_WORDS or re.match(r"^\d+$", candidate):
            continue
        return {"status": "invalid", "mention": candidate}

    return {"status": "none"}

def query_socrata(dataset: str, where: str = None, select: str = "count(*) AS total", limit: int = 10) -> dict:
    dataset_id = DATASETS.get(dataset)
    if not dataset_id:
        return {"error": f"Unknown dataset: {dataset}"}
    params = {"$select": select, "$limit": str(limit)}
    if where:
        params["$where"] = where
    if SOCRATA_APP_TOKEN:
        params["$$app_token"] = SOCRATA_APP_TOKEN
    url = f"{SOCRATA_BASE}/{dataset_id}.json?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10, context=_SSL_CTX) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        app.logger.error("[socrata] HTTP %s for url=%s body=%s", e.code, url, body)
        return {"error": f"HTTP Error {e.code}: {e.reason}", "detail": body}
    except Exception as e:
        app.logger.error("[socrata] error url=%s exc=%s", url, e)
        return {"error": str(e)}

def _build_socrata_tools() -> list:
    """Build the SOCRATA_TOOLS list, injecting the full community area mapping."""
    if COMMUNITY_AREA_BY_NUM:
        areas_str = "; ".join(
            f"{name}={num}" for num, name in sorted(COMMUNITY_AREA_BY_NUM.items())
        )
        community_area_note = (
            "community_area is a NUMBER — never a quoted name string.\n"
            f"Valid community areas: {areas_str}.\n"
            "If the user names a place that is NOT in this list, do not guess a number — "
            "tell the user that location is not a Chicago community area."
        )
    else:
        community_area_note = (
            "community_area is a NUMBER (e.g. West Loop=28, Loop=32). "
            "Do NOT filter by neighborhood name string."
        )

    return [
        {
            "name": "query_chicago_data",
            "description": (
                "Query the Chicago Open Data Portal for live statistics. "
                "ONLY call this tool for questions explicitly about one of the four available datasets: "
                "business_licenses, building_permits, crime, 311_requests. "
                "Do NOT call for schools, CPS enrollment, parks, transit, health, or any other topic."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "dataset": {
                        "type": "string",
                        "enum": ["business_licenses", "building_permits", "crime", "311_requests"],
                        "description": "Which dataset to query",
                    },
                    "where": {
                        "type": "string",
                        "description": (
                            "SODA $where clause using SoQL syntax. Examples:\n"
                            "  date >= '2025-01-01' AND date < '2026-01-01'\n"
                            "  year = '2025' AND primary_type = 'THEFT'\n"
                            f"IMPORTANT for crime/permit datasets: {community_area_note}\n"
                            "Crime date field is 'date'. Use date range for year filtering.\n"
                            "primary_type values are ALL CAPS strings like 'THEFT', 'ASSAULT', 'HOMICIDE'."
                        ),
                    },
                    "select": {
                        "type": "string",
                        "description": (
                            "SODA $select clause, e.g. \"count(*) AS total\" "
                            "or \"primary_type, count(*) AS total GROUP BY primary_type\""
                        ),
                    },
                },
                "required": ["dataset"],
            },
        }
    ]


SOCRATA_TOOLS: list = []   # rebuilt in load_resources() after community areas are loaded


def _ipv4_connect_params(dsn: str) -> dict:
    """Parse DSN and inject hostaddr (IPv4) so psycopg2 never tries IPv6."""
    params = psycopg2.extensions.parse_dsn(dsn)
    hostname = params.get("host", "")
    if hostname:
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
            params["hostaddr"] = infos[0][4][0]
            print(f"Resolved {hostname} → {params['hostaddr']} (IPv4)", flush=True)
        except Exception as e:
            print(f"IPv4 resolution failed for {hostname}: {e}", flush=True)
    return params

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
    global _voyage, _pool, SCRAPE_DATE, TOTAL_CHUNKS, SOCRATA_TOOLS

    _load_community_areas()
    SOCRATA_TOOLS = _build_socrata_tools()

    print("Initialising Voyage AI client...", flush=True)
    _voyage = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"], timeout=30)

    print("Connecting to Supabase...", flush=True)
    _conn_params = _ipv4_connect_params(DATABASE_URL)
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2, maxconn=10, connect_timeout=10, **_conn_params
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


def log_data_query(session_id, question, dataset, where_clause, select_clause, records_returned, raw_result):
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO data_query_log "
                "(session_id, question, dataset, where_clause, select_clause, records_returned, raw_result) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)",
                (session_id, question, dataset, where_clause, select_clause,
                 records_returned, json.dumps(raw_result)),
            )
    except Exception as exc:
        app.logger.error("DB log_data_query failed: %s", exc)


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


def _parse_intent(question: str, history: list = None) -> dict:
    """Use Claude Haiku to extract structured query intent.

    Passes recent conversation history so follow-up replies ('yes', 'the first one', etc.)
    are understood in context — no word-count heuristics needed.

    Returns a dict with keys:
      is_data_query, dataset, has_time, time_phrase,
      has_location, location_phrase, is_citywide
    """
    messages = []
    if history:
        for turn in history[-4:]:
            role    = turn.get("role", "")
            content = turn.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})

    try:
        resp = client.messages.create(
            model      = "claude-haiku-4-5-20251001",
            max_tokens = 256,
            system     = (
                "You extract structured intent from user questions about Chicago city data. "
                "The four available datasets are: crime incidents, building permits, "
                "business licenses, and 311 service requests. "
                "Always call the parse_data_query_intent tool."
            ),
            tools       = [INTENT_TOOL],
            tool_choice = {"type": "tool", "name": "parse_data_query_intent"},
            messages    = messages,
        )
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
    except Exception as exc:
        app.logger.warning("[intent] parse failed, defaulting to non-data: %s", exc)

    return {"is_data_query": False, "has_time": False, "has_location": False, "is_citywide": False}


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

    "Otherwise answer using ONLY information from the City of Chicago website (chicago.gov), "
    "Chicago Park District website (chicagoparkdistrict.com), or Chicago Public Schools website (cps.edu). "
    "Name the relevant department or organization when helpful. "
    "If the answer is not in any of those sources, say so and suggest chicago.gov, chicagoparkdistrict.com, cps.edu, or 311 as appropriate.\n\n"

    "HARD RULE — URLS: Never invent, guess, or construct a URL. Only use URLs "
    "that appear verbatim in the City of Chicago website content provided above. "
    "If no URL is present in that content, do not include any link in your response.\n\n"

    "DATA QUERIES: ONLY use the query_chicago_data tool when the question is EXPLICITLY about "
    "one of these four topics available on the Chicago Open Data Portal: business licenses, "
    "building permits, crime incidents, or 311 service requests. "
    "Do NOT use the tool for schools, enrollment, parks, libraries, transit, "
    "health, or any other topic — even if the question contains words like 'how many' or 'count'. "
    "For questions about Chicago Public Schools or Chicago Park District, use the RAG context from cps.edu or chicagoparkdistrict.com instead. "
    "When the tool IS appropriate, fetch live data and ONLY report the exact figures returned — "
    "do not estimate, extrapolate, or invent numbers. "
    "State the dataset name (e.g. 'The Chicago Open Data Portal crime dataset shows ...'). "
    "If the query returned no results or an error, say so explicitly. "
    "For data answers, skip the SOURCES line and instead cite 'data.cityofchicago.org'.\n\n"
    "COMMUNITY AREAS: Chicago's datasets use numeric community area codes. "
    "The valid community areas are listed in the query_chicago_data tool description. "
    "If a user asks about a neighborhood that is NOT in that list (e.g. a street, landmark, "
    "or informal name like 'River North' or 'Mag Mile'), explicitly tell them it is not a "
    "Chicago community area and suggest the closest valid community area if obvious.\n\n"

    "OUT-OF-SCOPE QUANTITATIVE QUESTIONS: If the user asks a quantitative question "
    "(e.g. 'how many', 'count', 'total', 'percentage') about a topic that is NOT one of the four "
    "Chicago Open Data Portal datasets (business licenses, building permits, crime, 311 requests), "
    "answer from the RAG context if possible, then add on a new line: "
    "'Note: This tool is still in development and is only scoped for a limited set of city data. "
    "If you find it helpful and want to see it improve, hit the thumbs up button below!'\n\n"

    "SOURCES LINE: After your answer, on a new line, write exactly:\n"
    "  SOURCES: <comma-separated list of the source URLs you actually used from the context>\n"
    "Always include the most specific source URL, or the top-level source used:\n"
    "chicago.gov for City of Chicago services, chicagoparkdistrict.com for parks information, cps.edu for Chicago Public Schools information.\n"
    "If you used no specific URL from the context, write: SOURCES: none\n"
    "Only list URLs that actually appear verbatim in the context provided."
)

DISCLAIMER_TEMPLATE = (
    "Information sourced from city websites as of {date}. "
    "Content may have changed — visit the sources directly to confirm."
)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_raw_log = logging.getLogger("raw_wsgi")


class RawLoggingMiddleware:
    """Fires before Flask routing — confirms the request reached Python at all."""
    def __init__(self, wsgi_app):
        self._app = wsgi_app

    def __call__(self, environ, start_response):
        import threading
        _raw_log.info(
            "[raw] %s %s  content_type=%r  content_length=%s  thread=%s",
            environ.get("REQUEST_METHOD"),
            environ.get("PATH_INFO"),
            environ.get("CONTENT_TYPE"),
            environ.get("CONTENT_LENGTH"),
            threading.current_thread().name,
        )
        return self._app(environ, start_response)


app = Flask(__name__, static_folder="static")
app.wsgi_app = RawLoggingMiddleware(app.wsgi_app)
app.logger.setLevel(logging.INFO)


@app.before_request
def log_request_start():
    app.logger.info(
        "[flask] %s %s  content_type=%r  content_length=%s  thread=%s",
        request.method,
        request.path,
        request.content_type,
        request.content_length,
        __import__("threading").current_thread().name,
    )


@app.after_request
def log_request_end(response):
    app.logger.info(
        "[flask] %s %s → %s",
        request.method,
        request.path,
        response.status_code,
    )
    return response


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
        app.logger.info("[chat] rejected: empty question")
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

    # 0. Pre-flight: parse intent with Claude to identify data queries and missing components
    #    before the expensive Voyage embedding + pgvector calls.
    app.logger.info("[chat] +%s parsing intent", elapsed())
    intent = _parse_intent(question, history)
    app.logger.info("[chat] intent: %s", intent)

    use_data_tool     = bool(intent.get("is_data_query"))
    resolved_area_num: int | None = None   # set when a valid community area is resolved

    def _clarify(text: str):
        upsert_turn(session_id, lang,
            {"role": "user", "content": question},
            {"role": "assistant", "content": text, "type": "clarification", "sources": []},
        )
        return jsonify({"type": "clarification", "answer": text, "sources": []})

    if use_data_tool:
        dataset           = intent.get("dataset")
        has_time          = intent.get("has_time", False)
        has_location      = intent.get("has_location", False)
        is_citywide       = intent.get("is_citywide", False)
        location_phrase   = intent.get("location_phrase", "")
        loc_supported     = DATASET_HAS_LOCATION.get(dataset, True)
        dataset_label     = DATASET_LABELS.get(dataset, dataset or "this dataset")

        # Ask for time period first if missing
        if not has_time:
            return _clarify(
                "What time period are you asking about? "
                "(e.g., 2024, last year, January–June 2025)"
            )

        # If dataset supports location and none was given, ask
        if loc_supported and not has_location:
            return _clarify(
                f"Which neighborhood in Chicago are you asking about, "
                f"or would you like data for all of Chicago?"
            )

        # If dataset does NOT support location but user asked for one, tell them
        if not loc_supported and has_location and not is_citywide:
            app.logger.info("[chat] location not supported for %s, ignoring", dataset)
            # Fall through — proceed without location filter; note will be in answer

        # Validate community area if a specific location was named
        if has_location and not is_citywide:
            loc = _check_location_in_query(location_phrase or question)
            app.logger.info("[chat] location check: %s", loc)

            if loc["status"] == "invalid":
                areas_list = ", ".join(sorted(COMMUNITY_AREA_BY_NUM.values()))
                return _clarify(
                    f"'{loc['mention']}' is not a Chicago community area. "
                    f"Please choose one of the 77 official community areas:\n\n{areas_list}"
                )
            elif loc["status"] == "valid":
                resolved_area_num = loc["num"]
                app.logger.info(
                    "[chat] resolved community area: %s → %d", loc["name"], loc["num"]
                )
            # "citywide" status from _check_location_in_query also falls through cleanly

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

    # Inject resolved community area so Claude uses the correct numeric code
    if resolved_area_num is not None:
        area_name = COMMUNITY_AREA_BY_NUM[resolved_area_num]
        user_content += (
            f"\n\n[LOCATION NOTE: '{area_name}' = community_area {resolved_area_num}. "
            f"Use community_area={resolved_area_num} (bare integer, no quotes) in any Socrata WHERE clause.]"
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

    data_query_meta = None
    tool_result     = None

    active_tools = SOCRATA_TOOLS if use_data_tool else []
    app.logger.info("[route] question=%r  data_tool=%s", question, use_data_tool)

    app.logger.info("[chat] +%s calling Anthropic API", elapsed())
    try:
        message = client.messages.create(
            model      = "claude-haiku-4-5-20251001",
            max_tokens = 400,
            system     = SYSTEM_PROMPT,
            tools      = SOCRATA_TOOLS,
            messages   = messages,
        )
        app.logger.info("[chat] +%s Anthropic API done (stop_reason=%s)", elapsed(), message.stop_reason)

        # Handle tool use — Claude wants to query the Chicago Data Portal
        if message.stop_reason == "tool_use":
            tool_use_block = next(b for b in message.content if b.type == "tool_use")
            tool_input     = tool_use_block.input
            dataset  = tool_input.get("dataset", "")
            where    = tool_input.get("where") or ""
            select   = tool_input.get("select") or "count(*) AS total"

            # Safety net: translate any quoted community area names to numeric codes
            if where and COMMUNITY_AREA_BY_NAME:
                translated = _translate_community_areas_in_where(where)
                if translated != where:
                    app.logger.info("[tool_use] translated where: %r → %r", where, translated)
                    where = translated

            app.logger.info(
                "[tool_use] question=%r  dataset=%s  where=%r  select=%r",
                question, dataset, where, select,
            )

            tool_result = query_socrata(
                dataset=dataset,
                where=where or None,
                select=select,
            )
            records = len(tool_result) if isinstance(tool_result, list) else None
            app.logger.info(
                "[tool_result] dataset=%s  records=%s  result=%s",
                dataset, records, str(tool_result)[:300],
            )

            # If Socrata returned an error, skip the second LLM call entirely
            # and return a graceful response from the RAG context instead.
            if isinstance(tool_result, dict) and "error" in tool_result:
                app.logger.info("[tool_result] Socrata error — falling back to RAG: %s", tool_result["error"])
                # Make a new call with tools disabled so Claude answers from RAG context.
                fallback_msg = client.messages.create(
                    model      = "claude-haiku-4-5-20251001",
                    max_tokens = 400,
                    system     = SYSTEM_PROMPT,
                    tool_choice= {"type": "none"},
                    tools      = SOCRATA_TOOLS,
                    messages   = [messages[-1]],   # just the user turn with RAG context
                )
                app.logger.info("[chat] +%s fallback RAG call done", elapsed())
                message = fallback_msg
            else:
                data_query_meta = {
                    "dataset":          dataset,
                    "where":            where,
                    "select":           select,
                    "records_returned": records or 0,
                }

                messages.append({"role": "assistant", "content": message.content})
                messages.append({
                    "role": "user",
                    "content": [{
                        "type":        "tool_result",
                        "tool_use_id": tool_use_block.id,
                        "content":     json.dumps(tool_result),
                    }],
                })
                message = client.messages.create(
                    model      = "claude-haiku-4-5-20251001",
                    max_tokens = 400,
                    system     = SYSTEM_PROMPT,
                    tools      = SOCRATA_TOOLS,
                    messages   = messages,
                )
                app.logger.info("[chat] +%s Anthropic API (2nd call) done", elapsed())

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

    text_block = next((b for b in message.content if hasattr(b, "text")), None)
    if not text_block:
        app.logger.warning(
            "[chat] no text block in response (stop_reason=%s content=%r)",
            message.stop_reason, message.content,
        )
        return jsonify({
            "type"   : "answer",
            "answer" : "Sorry, I couldn't produce an answer. Please try again.",
            "sources": [],
        })
    raw = text_block.text.strip()

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
    if data_query_meta:
        log_data_query(
            session_id, question,
            data_query_meta["dataset"], data_query_meta["where"],
            data_query_meta["select"],  data_query_meta["records_returned"],
            tool_result,
        )
    assistant_turn = {
        "role": "assistant", "content": answer_text, "type": "answer",
        "sources": [s["url"] for s in filtered_sources],
        "used_urls": sorted(used_urls),
        "fallback_used": fallback_used,
    }
    if data_query_meta:
        assistant_turn["data_query"] = data_query_meta
    upsert_turn(session_id, lang,
        {"role": "user", "content": question},
        assistant_turn,
    )
    app.logger.info("[chat] +%s done", elapsed())

    # For data queries, replace RAG sources with the Chicago Data Portal dataset URL
    if data_query_meta:
        dataset_id = DATASETS.get(data_query_meta["dataset"], "")
        filtered_sources = [
            {"title": "Chicago Open Data Portal", "url": "https://data.cityofchicago.org"},
            {"title": f"Dataset: {data_query_meta['dataset']} ({dataset_id})",
             "url": f"https://data.cityofchicago.org/resource/{dataset_id}"},
        ]

    response = {
        "type"       : "answer",
        "answer"     : answer_text,
        "sources"    : filtered_sources,
        "scrape_date": SCRAPE_DATE,
        "disclaimer" : DISCLAIMER_TEMPLATE.format(date=SCRAPE_DATE),
    }
    if data_query_meta:
        response["data_query"] = data_query_meta
    return jsonify(response)


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
