import logging

from flask import Blueprint, jsonify, request

from ..data_sources.legiscan import (
    claude_rerank, enrich_bill, get_bill_detail, get_recent_bills,
    plain_language_titles, search_bills,
)

logger = logging.getLogger(__name__)
bp = Blueprint("illinois_legislation", __name__)


@bp.route("/illinois/legislation/recent")
def il_legislation_recent():
    try:
        bills = get_recent_bills()[:12]
        pt = plain_language_titles(bills)
        slim = [
            {
                "bill_id":            b.get("bill_id"),
                "bill_number":        b.get("bill_number"),
                "title":              b.get("title") or b.get("description"),
                "plainLanguageTitle": pt.get(str(b.get("bill_id"))),
                "status":             b.get("status"),
                "last_action":        b.get("last_action"),
                "last_action_date":   b.get("last_action_date"),
                "url":                b.get("url"),
            }
            for b in bills
        ]
        return jsonify({"bills": slim, "count": len(slim)})
    except Exception as e:
        logger.error("[il_legislation] recent error: %s", e)
        return jsonify({"bills": [], "count": 0})


@bp.route("/illinois/legislation/search")
def il_legislation_search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"bills": [], "count": 0})
    try:
        bills = search_bills(q)
        bills = claude_rerank(q, bills)[:10]
        pt = plain_language_titles(bills)
        slim = [
            {
                "bill_id":            b.get("bill_id"),
                "bill_number":        b.get("bill_number"),
                "title":              b.get("title") or b.get("description"),
                "plainLanguageTitle": pt.get(str(b.get("bill_id"))),
                "status":             b.get("status"),
                "last_action":        b.get("last_action"),
                "last_action_date":   b.get("last_action_date"),
                "url":                b.get("url"),
            }
            for b in bills
        ]
        return jsonify({"bills": slim, "count": len(slim)})
    except Exception as e:
        logger.error("[il_legislation] search error: %s", e)
        return jsonify({"error": "Search failed. Please try again."}), 500


@bp.route("/illinois/legislation/bills/<bill_id>")
def il_legislation_bill(bill_id):
    try:
        bill = get_bill_detail(int(bill_id))
        if not bill:
            return jsonify({"error": "Bill not found."}), 404
        pt = plain_language_titles([bill])
        bill["plainLanguageTitle"] = pt.get(str(bill.get("bill_id")))
        return jsonify(bill)
    except Exception as e:
        logger.error("[il_legislation] bill fetch error %s: %s", bill_id, e)
        return jsonify({"error": "Could not load bill."}), 500
