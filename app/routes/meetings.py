import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from ..data_sources.elms import (
    fetch_meeting_items, fetch_matter_detail_slim,
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
        if len(results) >= 5:
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

        items = fetch_meeting_items(meeting_id)
        valid_items = [i for i in items if i.get("recordNumber")]

        sorted_items = (
            [i for i in valid_items if not i.get("isRoutine")]
            + [i for i in valid_items if i.get("isRoutine")]
        )
        total = len(sorted_items)
        page  = sorted_items[offset: offset + limit]

        detail_map = {}
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {
                pool.submit(fetch_matter_detail_slim, i["recordNumber"]): i["recordNumber"]
                for i in page
            }
            for fut in as_completed(futures):
                d = fut.result()
                detail_map[d["recordNumber"]] = d

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
        return jsonify({"matters": slim, "total": total, "offset": offset, "limit": limit})
    except Exception as e:
        logger.error("[meetings] matters error %s: %s", meeting_id, e)
        return jsonify({"matters": [], "total": 0, "offset": 0, "limit": 10})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _meetings_from_db(limit: int = 5) -> list[dict]:
    """Read enriched meetings directly from DB — no ELMS call needed."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT ms.meeting_id, ms.body, ms.meeting_date, ms.summary,
                          km.location, km.elms_status, km.nonroutine_count, km.routine_count
                   FROM meeting_summaries ms
                   LEFT JOIN known_meetings km USING (meeting_id)
                   WHERE ms.meeting_date IS NOT NULL AND ms.meeting_date <= %s
                   ORDER BY ms.meeting_date DESC
                   LIMIT %s""",
                (today, limit),
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.warning("[meetings] DB list query failed: %s", e)
        return []

    results = []
    for meeting_id, body, meeting_date, summary, location, elms_status, nonroutine_count, routine_count in rows:
        nonroutine_count = nonroutine_count or 0
        routine_count    = routine_count or 0
        results.append({
            "meetingId":       meeting_id,
            "date":            meeting_date,
            "body":            body or "",
            "location":        location or "",
            "status":          elms_status or "",
            "summary":         summary,
            "routineCount":    routine_count,
            "nonRoutineCount": nonroutine_count,
            "totalCount":      nonroutine_count + routine_count,
        })
    return results


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
    if not items:
        return None
    routine_count = sum(1 for i in items if i.get("isRoutine"))
    non_routine_count = len(items) - routine_count
    date_str = (m.get("date") or "")[:10]
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
