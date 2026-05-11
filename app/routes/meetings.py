import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from ..data_sources.elms import (
    _classify_routine, _s_variant,
    fetch_meeting_items, fetch_matter_detail_slim,
    get_meeting_document_summaries,
    meeting_summary, plain_language_titles,
)
from ..db import _db

logger = logging.getLogger(__name__)
bp = Blueprint("meetings", __name__)

_cache: dict = {}          # key → {"ts": float, "data": dict}
_RECENT_TTL  = 15 * 60    # 15 min — fast reload, still gets new meetings same day
_ALL_TTL     = 30 * 60    # 30 min for the full list


@bp.route("/meetings/recent")
def meetings_recent():
    cached = _cache.get("recent")
    if cached and time.time() - cached["ts"] < _RECENT_TTL:
        return jsonify(cached["data"])
    try:
        results = _meetings_from_db(limit=5)
        if results:
            _trigger_missing_summaries(results)
            payload = {"meetings": results}
            _cache["recent"] = {"ts": time.time(), "data": payload}
            return jsonify(payload)

        # DB doesn't have enough — fall back to ELMS (new meetings not yet processed by scheduler)
        today = datetime.now(timezone.utc).date().isoformat()
        raw = _elms_get_meetings()
        meetings = raw.get("value", raw.get("data", []))
        seen = set()
        past = []
        for m in meetings:
            date_str = (m.get("date") or "")[:10]
            if not date_str or date_str > today or not m.get("meetingId"):
                continue
            key = (date_str, m.get("body", ""))
            if key not in seen:
                seen.add(key)
                past.append(m)
                # Seed known_meetings so the next cache-miss is DB-only
                _seed_known_meeting(m)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_enrich_meeting, m): m for m in past}
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                        if len(results) >= 5:
                            for f in futures:
                                f.cancel()
                            break
                except Exception:
                    pass

        results.sort(key=lambda x: x.get("date") or "", reverse=True)
        payload = {"meetings": results[:5]}
        _cache["recent"] = {"ts": time.time(), "data": payload}
        return jsonify(payload)
    except Exception as e:
        logger.error("[meetings] recent error: %s", e)
        return jsonify({"meetings": []})


@bp.route("/meetings/all")
def meetings_all():
    cached = _cache.get("all")
    if cached and time.time() - cached["ts"] < _ALL_TTL:
        return jsonify(cached["data"])
    try:
        results = _meetings_from_db(limit=50)
        _trigger_missing_summaries(results)
        payload = {"meetings": results}
        _cache["all"] = {"ts": time.time(), "data": payload}
        return jsonify(payload)
    except Exception as e:
        logger.error("[meetings] all error: %s", e)
        return jsonify({"meetings": []})


@bp.route("/meetings/<path:meeting_id>/matters")
def meeting_matters(meeting_id):
    try:
        offset = max(0, int(request.args.get("offset", 0)))
        limit  = min(50, max(1, int(request.args.get("limit", 10))))

        # Determine if this meeting is upcoming before any heavy work
        today = datetime.now(timezone.utc).date().isoformat()
        _, meeting_date_str = _get_meeting_meta(meeting_id)
        is_upcoming = bool(meeting_date_str and meeting_date_str > today)

        cached = _items_from_cache(meeting_id)
        if cached:
            items = cached
            # Refresh in background — picks up any items added to ELMS after last cache write
            threading.Thread(
                target=_refresh_meeting_items, args=(meeting_id,), daemon=True
            ).start()
        else:
            items = fetch_meeting_items(meeting_id)
            if items:
                _write_items_to_db(meeting_id, items)
            else:
                # ELMS agenda endpoint has no published items for this meeting.
                # Reconstruct from matter_detail_cache by matching action date + committee.
                items = _reconstruct_items_from_actions(meeting_id)
                if items:
                    _write_items_to_db(meeting_id, items)
        valid_items = [i for i in items if i.get("recordNumber")]

        if not valid_items:
            docs = get_meeting_document_summaries(meeting_id)
            summary = None if is_upcoming else _get_meeting_summary(meeting_id, items)
            return jsonify({
                "matters": [],
                "total": 0,
                "offset": 0,
                "limit": limit,
                "hasNoMatters": True,
                "isUpcoming": is_upcoming,
                "meetingDocuments": docs,
                "meetingSummary": summary,
            })

        sorted_items = (
            [i for i in valid_items if not i.get("isRoutine")]
            + [i for i in valid_items if i.get("isRoutine")]
        )
        total = len(sorted_items)
        page  = sorted_items[offset: offset + limit]

        detail_map = {}
        docs_future = None
        summary_future = None
        with ThreadPoolExecutor(max_workers=7) as pool:
            if offset == 0:
                docs_future = pool.submit(get_meeting_document_summaries, meeting_id)
                if not is_upcoming:
                    summary_future = pool.submit(_get_meeting_summary, meeting_id, items)
            futures = {
                pool.submit(_slim_from_cache, i["recordNumber"]): i["recordNumber"]
                for i in page
            }
            for fut in as_completed(futures):
                d = fut.result()
                detail_map[d["recordNumber"]] = d
            docs    = docs_future.result()    if docs_future    else []
            summary = summary_future.result() if summary_future else None

        matter_stubs = [
            {"recordNumber": i.get("recordNumber"), "title": i.get("matterTitle")}
            for i in page
        ]
        pt = plain_language_titles(matter_stubs)
        slim = []
        for i in page:
            rn = i.get("recordNumber")
            detail = detail_map.get(rn, {})
            slim.append({
                "recordNumber": rn,
                "matterId": i.get("matterId"),
                "title": i.get("matterTitle"),
                "plainLanguageTitle": pt.get(rn),
                "type": i.get("matterType"),
                "status": detail.get("status"),
                "substatus": detail.get("substatus"),
                "introductionDate": detail.get("introductionDate"),
                "controllingBody": detail.get("controllingBody"),
                "actionName": i.get("actionName"),
                "isRoutine": i.get("isRoutine", False),
            })
        payload = {"matters": slim, "total": total, "offset": offset, "limit": limit,
                   "isUpcoming": is_upcoming}
        if offset == 0:
            payload["meetingDocuments"] = docs
            payload["meetingSummary"] = summary
        return jsonify(payload)
    except Exception as e:
        logger.error("[meetings] matters error %s: %s", meeting_id, e)
        return jsonify({"matters": [], "total": 0, "offset": 0, "limit": 10})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _meetings_from_db(limit: int = 5) -> list[dict]:
    """Read enriched meetings directly from DB — no ELMS call needed.

    Returns past meetings (most-recent first) followed by upcoming meetings
    (soonest first), each tagged with isUpcoming.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    future_cutoff = (datetime.now(timezone.utc).date().isoformat())  # 30-day window handled by known_meetings sync
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """(
                     SELECT km.meeting_id, km.body, km.meeting_date, ms.summary,
                            km.location, km.elms_status, km.nonroutine_count, km.routine_count,
                            FALSE AS is_upcoming
                     FROM known_meetings km
                     LEFT JOIN meeting_summaries ms USING (meeting_id)
                     WHERE km.meeting_date <= %s
                     ORDER BY km.meeting_date DESC
                     LIMIT %s
                   )
                   UNION ALL
                   (
                     SELECT km.meeting_id, km.body, km.meeting_date, NULL AS summary,
                            km.location, km.elms_status, km.nonroutine_count, km.routine_count,
                            TRUE AS is_upcoming
                     FROM known_meetings km
                     WHERE km.meeting_date > %s
                     ORDER BY km.meeting_date ASC
                     LIMIT 30
                   )""",
                (today, limit, today),
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("[meetings] DB list query failed: %s", e)
        return []

    results = []
    for meeting_id, body, meeting_date, summary, location, elms_status, nonroutine_count, routine_count, is_upcoming in rows:
        nonroutine_count = nonroutine_count or 0
        routine_count    = routine_count or 0
        results.append({
            "meetingId":       meeting_id,
            "date":            meeting_date,
            "body":            body or "",
            "location":        location or "",
            "status":          elms_status or "",
            "summary":         None if is_upcoming else summary,
            "isUpcoming":      bool(is_upcoming),
            "routineCount":    routine_count,
            "nonRoutineCount": nonroutine_count,
            "totalCount":      nonroutine_count + routine_count,
        })
    return results


def _items_from_cache(meeting_id: str) -> list[dict]:
    """Read agenda items from meeting_items table; returns [] on miss so caller falls back to ELMS."""
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT record_number, matter_id, matter_title, matter_type, action_name, is_routine
                   FROM meeting_items WHERE meeting_id = %s
                   ORDER BY is_routine ASC, item_order ASC""",
                (meeting_id,),
            )
            rows = cur.fetchall()
            return [
                {
                    "recordNumber": r[0],
                    "matterId":     r[1],
                    "matterTitle":  r[2],
                    "matterType":   r[3],
                    "actionName":   r[4],
                    "isRoutine":    r[5],
                }
                for r in rows
            ]
    except Exception as e:
        logger.warning("[meetings] items_from_cache %s: %s", meeting_id, e)
        return []


def _slim_from_cache(record_number: str) -> dict:
    """Read slim matter fields from matter_detail_cache; fall back to ELMS only on cache miss."""
    s_number = _s_variant(record_number)
    candidates = [record_number] if s_number == record_number else [record_number, s_number]
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT status, data FROM matter_detail_cache
                   WHERE record_number = ANY(%s)
                   ORDER BY (status IS NOT NULL) DESC, cached_at DESC
                   LIMIT 1""",
                (candidates,),
            )
            row = cur.fetchone()
            if row:
                status, data = row
                d = data if isinstance(data, dict) else json.loads(data)
                return {
                    "recordNumber": record_number,
                    "status": d.get("status") or status,
                    "substatus": d.get("subStatus") or d.get("substatus"),
                    "introductionDate": d.get("introductionDate"),
                    "controllingBody": d.get("controllingBody"),
                }
    except Exception as e:
        logger.warning("[meetings] slim cache read failed for %s: %s", record_number, e)
    # Try S-variant fallback on ELMS if canonical fails
    try:
        return fetch_matter_detail_slim(record_number)
    except Exception:
        if s_number != record_number:
            return fetch_matter_detail_slim(s_number)
        raise


def _seed_known_meeting(m: dict) -> None:
    """Insert a meeting stub into known_meetings so future loads skip ELMS."""
    mid      = m.get("meetingId")
    body     = m.get("body") or ""
    date_str = (m.get("date") or "")[:10]
    status   = (m.get("status") or "").strip()
    if not mid or not body or not date_str:
        return
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO known_meetings (meeting_id, body, meeting_date, elms_status)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (meeting_id) DO NOTHING""",
                (mid, body, date_str, status),
            )
    except Exception as e:
        logger.warning("[meetings] seed_known_meeting %s: %s", mid, e)


def _trigger_missing_summaries(meetings: list[dict]) -> None:
    """Fire a background thread to generate summaries for past meetings that lack one."""
    today = datetime.now(timezone.utc).date().isoformat()
    unsummarized = [
        m for m in meetings
        if not m.get("summary") and (m.get("date") or "") <= today
    ]
    if unsummarized:
        t = threading.Thread(target=_generate_missing_summaries, args=(unsummarized,), daemon=True)
        t.start()


def _generate_missing_summaries(meetings: list[dict]) -> None:
    """Generate and cache meeting summaries in the background.

    For meetings with agenda items: summarizes the items.
    For meetings with no items: summarizes meeting documents (handled inside meeting_summary).
    Clears the in-process route cache after all summaries are written so the next
    request picks them up.
    """
    for m in meetings:
        meeting_id = m.get("meetingId")
        body       = m.get("body", "")
        date_str   = (m.get("date") or "")[:10]
        if not meeting_id:
            continue
        try:
            items = _items_from_cache(meeting_id)
            if not items:
                items = fetch_meeting_items(meeting_id)
            meeting_summary(meeting_id, body, date_str, items)
        except Exception as e:
            logger.warning("[meetings] bg summary gen failed for %s: %s", meeting_id, e)
    # Bust the in-memory cache so the next /meetings/recent or /all load sees new summaries
    _cache.pop("recent", None)
    _cache.pop("all", None)


def _reconstruct_items_from_actions(meeting_id: str) -> list[dict]:
    """For meetings where ELMS publishes no agenda items, find matters by matching
    action date + committee body from matter_detail_cache."""
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT body, meeting_date FROM known_meetings WHERE meeting_id = %s",
                (meeting_id,),
            )
            row = cur.fetchone()
            if not row:
                return []
            body, meeting_date = row
            date_str = str(meeting_date)  # already a date object from psycopg2

            cur.execute(
                """SELECT record_number, data
                   FROM matter_detail_cache
                   WHERE EXISTS (
                     SELECT 1 FROM jsonb_array_elements(data->'actions') AS a
                     WHERE (a->>'actionDate')::date
                             BETWEEN (%s::date - INTERVAL '1 day')
                                 AND (%s::date + INTERVAL '1 day')
                       AND (a->>'actionByName') ILIKE %s
                   )""",
                (date_str, date_str, f"%{body}%"),
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("[meetings] reconstruct_items_from_actions %s: %s", meeting_id, e)
        return []

    items = []
    for rn, data in rows:
        d = data if isinstance(data, dict) else {}
        matter_type = d.get("type") or ""
        matter_title = d.get("title") or d.get("shortTitle") or ""
        # Find the closest matching action for actionName
        action_name = ""
        for a in (d.get("actions") or []):
            a_date = (a.get("actionDate") or "")[:10]
            a_body = a.get("actionByName") or ""
            if body.lower() in a_body.lower():
                action_name = a.get("actionName") or ""
                break
        items.append({
            "recordNumber": rn,
            "matterId": d.get("matterId"),
            "matterTitle": matter_title,
            "matterType": matter_type,
            "actionName": action_name,
            "isRoutine": _classify_routine(matter_type, matter_title),
        })
    return items


def _write_items_to_db(meeting_id: str, items: list[dict]) -> None:
    """Upsert agenda items into meeting_items — safe to call multiple times."""
    if not items:
        return
    try:
        with _db() as conn:
            cur = conn.cursor()
            for idx, item in enumerate(items):
                rn = item.get("recordNumber")
                if not rn:
                    continue
                cur.execute(
                    """INSERT INTO meeting_items
                           (meeting_id, record_number, matter_id, matter_title,
                            matter_type, action_name, is_routine, item_order)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (meeting_id, record_number) DO UPDATE
                         SET matter_title = EXCLUDED.matter_title,
                             matter_type  = EXCLUDED.matter_type,
                             action_name  = EXCLUDED.action_name,
                             is_routine   = EXCLUDED.is_routine,
                             item_order   = EXCLUDED.item_order,
                             cached_at    = NOW()""",
                    (meeting_id, rn, item.get("matterId"), item.get("matterTitle"),
                     item.get("matterType"), item.get("actionName"),
                     bool(item.get("isRoutine", False)), idx),
                )
    except Exception as e:
        logger.warning("[meetings] write_items_to_db %s: %s", meeting_id, e)


def _refresh_meeting_items(meeting_id: str) -> None:
    """Re-fetch meeting items from ELMS and upsert — adds items missed by the initial cache write."""
    try:
        items = fetch_meeting_items(meeting_id)
        _write_items_to_db(meeting_id, items)
    except Exception as e:
        logger.warning("[meetings] refresh_meeting_items %s: %s", meeting_id, e)


def _elms_get_meetings(top: int = 100):
    from ..data_sources.elms import _elms_get
    return _elms_get("/meeting-agenda", {"top": top, "orderby": "date desc"})


def _enrich_meeting(m: dict):
    meeting_id = m["meetingId"]

    # Fast path: if summary is already cached in DB, skip the ELMS items call entirely.
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT ms.summary, km.nonroutine_count
                   FROM meeting_summaries ms
                   LEFT JOIN known_meetings km USING (meeting_id)
                   WHERE ms.meeting_id = %s""",
                (meeting_id,),
            )
            row = cur.fetchone()
            if row:
                cached_summary, nonroutine_count = row
                nonroutine_count = nonroutine_count or 0
                return {
                    "meetingId": meeting_id,
                    "date": m.get("date"),
                    "body": m.get("body", ""),
                    "location": m.get("location", ""),
                    "status": m.get("status", ""),
                    "summary": cached_summary,
                    "routineCount": 0,
                    "nonRoutineCount": nonroutine_count,
                    "totalCount": nonroutine_count,
                }
    except Exception as e:
        logger.warning("[meetings] DB summary lookup failed for %s: %s", meeting_id, e)

    # Slow path: fetch items from ELMS, generate summary (which caches it), return enriched dict.
    try:
        items = fetch_meeting_items(meeting_id)
    except Exception:
        items = []
    routine_count = sum(1 for i in items if i.get("isRoutine"))
    non_routine_count = len(items) - routine_count
    date_str = (m.get("date") or "")[:10]
    # meeting_summary handles the no-items case by summarizing meeting documents
    summary = meeting_summary(meeting_id, m.get("body", ""), date_str, items)
    return {
        "meetingId": meeting_id,
        "date": m.get("date"),
        "body": m.get("body", ""),
        "location": m.get("location", ""),
        "status": m.get("status", ""),
        "summary": summary,
        "routineCount": routine_count,
        "nonRoutineCount": non_routine_count,
        "totalCount": len(items),
    }


def _get_meeting_meta(meeting_id: str) -> tuple[str, str]:
    """Return (body, date_str) from known_meetings, or empty strings if not found."""
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT body, meeting_date FROM known_meetings WHERE meeting_id = %s",
                (meeting_id,),
            )
            row = cur.fetchone()
            if row:
                return (row[0] or "", (row[1] or "")[:10])
    except Exception:
        pass
    return ("", "")


def _get_meeting_summary(meeting_id: str, items: list) -> str | None:
    """Return meeting summary, generating via Claude if not yet cached."""
    body, date_str = _get_meeting_meta(meeting_id)
    result = meeting_summary(meeting_id, body, date_str, items)
    return result or None
