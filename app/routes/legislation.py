import json
import logging

from flask import Blueprint, jsonify, request

from ..data_sources.elms import (
    _elms_get, _is_boilerplate, claude_rerank, get_enriched_matter, plain_language_titles,
)
from ..db import _db

logger = logging.getLogger(__name__)
bp = Blueprint("legislation", __name__)


@bp.route("/legislation/recent")
def legislation_recent():
    try:
        matters = _recent_from_cache(limit=12)
        if matters:
            return jsonify({"matters": matters, "count": len(matters)})
    except Exception as e:
        logger.warning("[legislation] recent cache failed, falling back to ELMS: %s", e)
    try:
        raw = _elms_get("/search", {"search": "", "top": 50, "orderby": "introductionDate desc"})
        matters = raw.get("value", raw.get("data", []))
        matters = [m for m in matters if not _is_boilerplate(m)][:12]
        pt = plain_language_titles(matters)
        slim = [
            {
                "recordNumber": m.get("recordNumber"),
                "title": m.get("title"),
                "plainLanguageTitle": pt.get(m.get("recordNumber")),
                "status": m.get("status"),
                "substatus": m.get("substatus"),
                "type": m.get("type"),
                "introductionDate": m.get("introductionDate"),
                "controllingBody": m.get("controllingBody"),
            }
            for m in matters
        ]
        return jsonify({"matters": slim, "count": len(slim)})
    except Exception as e:
        logger.error("[legislation] recent error: %s", e)
        return jsonify({"matters": [], "count": 0})


def _recent_from_cache(limit: int = 12) -> list[dict]:
    """Read recently introduced matters from matter_detail_cache ordered by introductionDate."""
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT data FROM matter_detail_cache
                   WHERE cached_at > NOW() - INTERVAL '60 days'
                   ORDER BY (data->>'introductionDate') DESC NULLS LAST
                   LIMIT %s""",
                (limit * 3,),  # over-fetch so boilerplate filtering doesn't leave us short
            )
            rows = cur.fetchall()
            matters = []
            for (data,) in rows:
                d = data if isinstance(data, dict) else json.loads(data)
                if _is_boilerplate({"title": d.get("title", "")}):
                    continue
                matters.append({
                    "recordNumber":      d.get("recordNumber"),
                    "title":             d.get("title"),
                    "plainLanguageTitle": d.get("plainLanguageTitle"),
                    "status":            d.get("status"),
                    "substatus":         d.get("subStatus") or d.get("substatus"),
                    "type":              d.get("type"),
                    "introductionDate":  d.get("introductionDate"),
                    "controllingBody":   d.get("controllingBody"),
                })
                if len(matters) >= limit:
                    break
            return matters
    except Exception as e:
        logger.warning("[legislation] recent_from_cache: %s", e)
        return []


@bp.route("/legislation/search")
def legislation_search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"matters": [], "count": 0})
    try:
        raw = _elms_get("/search", {"search": q, "top": 25})
        matters = raw.get("value", raw.get("data", []))
        matters = [m for m in matters if not _is_boilerplate(m)]
        matters = claude_rerank(q, matters)[:10]
        pt = plain_language_titles(matters)
        slim = [
            {
                "recordNumber": m.get("recordNumber"),
                "title": m.get("title"),
                "plainLanguageTitle": pt.get(m.get("recordNumber")),
                "status": m.get("status"),
                "substatus": m.get("subStatus") or m.get("substatus"),
                "type": m.get("type"),
                "introductionDate": m.get("introductionDate"),
                "controllingBody": m.get("controllingBody"),
            }
            for m in matters
        ]
        return jsonify({"matters": slim, "count": len(slim)})
    except Exception as e:
        logger.error("[legislation] search error: %s", e)
        return jsonify({"error": "Search failed. Please try again."}), 500


@bp.route("/legislation/matters/<path:record_number>")
def legislation_matter(record_number):
    try:
        matter = get_enriched_matter(record_number)
        return jsonify(matter)
    except Exception as e:
        logger.error("[legislation] matter fetch error %s: %s", record_number, e)
        return jsonify({"error": "Could not load matter."}), 500
