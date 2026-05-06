import json
import logging
from datetime import datetime, timezone

from .db import _db

logger = logging.getLogger(__name__)


def save_last_intent(session_id: str, intent: dict | None) -> None:
    if not session_id:
        return
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO sessions (session_id, lang, conversation, last_intent) "
                "VALUES (%s, '', '[]'::jsonb, %s::jsonb) "
                "ON CONFLICT (session_id) DO UPDATE SET last_intent = EXCLUDED.last_intent",
                (session_id, json.dumps(intent) if intent is not None else None),
            )
    except Exception as exc:
        logger.error("DB save_last_intent failed: %s", exc)


def get_last_intent(session_id: str) -> dict | None:
    if not session_id:
        return None
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT last_intent FROM sessions WHERE session_id = %s", (session_id,)
            )
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
    except Exception as exc:
        logger.error("DB get_last_intent failed: %s", exc)
    return None


def upsert_turn(session_id, lang, user_turn, assistant_turn):
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
        logger.error("DB upsert_turn failed: %s", exc)


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
        logger.error("DB log_source_debug failed: %s", exc)


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
        logger.error("DB log_data_query failed: %s", exc)


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
        logger.error("DB feedback update failed: %s", exc)
