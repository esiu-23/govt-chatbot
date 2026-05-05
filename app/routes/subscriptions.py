"""Routes for meeting and matter email subscriptions."""

import logging
import secrets

from flask import Blueprint, jsonify, make_response, request

from ..data_sources.elms import _COMMITTEE_CHAIRS
from ..db import _db
from ..email.sender import send_email
from ..email.templates import BASE_URL

logger = logging.getLogger(__name__)
bp = Blueprint("subscriptions", __name__)

_SUBSCRIBABLE_BODIES = ["City Council"] + sorted(_COMMITTEE_CHAIRS.keys())


@bp.route("/subscribe/bodies")
def subscribe_bodies():
    return jsonify({"bodies": _SUBSCRIBABLE_BODIES})


@bp.route("/subscribe/meetings", methods=["POST"])
def subscribe_meetings():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    body = (data.get("body") or "").strip()

    if not email or "@" not in email:
        return jsonify({"error": "Valid email required"}), 400
    if body not in _SUBSCRIBABLE_BODIES:
        return jsonify({"error": "Unknown body", "valid": _SUBSCRIBABLE_BODIES}), 400

    confirm_token = secrets.token_urlsafe(24)
    unsub_token = secrets.token_urlsafe(24)

    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO meeting_subscriptions (email, body, confirm_token, unsub_token)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (email, body) DO NOTHING""",
                (email, body, confirm_token, unsub_token),
            )
            if cur.rowcount == 0:
                # Already exists — check if confirmed
                cur.execute(
                    "SELECT confirmed, confirm_token FROM meeting_subscriptions WHERE email=%s AND body=%s",
                    (email, body),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return jsonify({"already_confirmed": True, "message": "You're already subscribed and confirmed."}), 200
                if row and not row[0]:
                    # Unconfirmed — resend using the existing token
                    confirm_token = row[1]
                    confirm_url = f"{BASE_URL}/subscribe/confirm/{confirm_token}"
                    _send_confirmation_email(email, f"{body} meeting updates", confirm_url)
                    return jsonify({"message": "Confirmation email resent — check your inbox."}), 202
    except Exception as e:
        logger.error("[subscribe] meetings insert error: %s", e)
        return jsonify({"error": "Database error"}), 500

    confirm_url = f"{BASE_URL}/subscribe/confirm/{confirm_token}"
    ok = _send_confirmation_email(email, f"{body} meeting updates", confirm_url)
    if not ok:
        logger.error("[subscribe] confirmation email failed for %s", email)
        return jsonify({"error": "Subscription saved but confirmation email failed. Please try again shortly."}), 500
    return jsonify({"message": "Check your email to confirm your subscription"}), 202


@bp.route("/subscribe/matters", methods=["POST"])
def subscribe_matters():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    record_number = (data.get("record_number") or "").strip().upper()

    if not email or "@" not in email:
        return jsonify({"error": "Valid email required"}), 400
    if not record_number:
        return jsonify({"error": "record_number required"}), 400

    confirm_token = secrets.token_urlsafe(24)
    unsub_token = secrets.token_urlsafe(24)

    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO matter_subscriptions (email, record_number, confirm_token, unsub_token)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (email, record_number) DO NOTHING""",
                (email, record_number, confirm_token, unsub_token),
            )
            if cur.rowcount == 0:
                cur.execute(
                    "SELECT confirmed, confirm_token FROM matter_subscriptions WHERE email=%s AND record_number=%s",
                    (email, record_number),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return jsonify({"already_confirmed": True, "message": "You're already tracking this matter."}), 200
                if row and not row[0]:
                    confirm_token = row[1]
                    confirm_url = f"{BASE_URL}/subscribe/confirm/{confirm_token}"
                    _send_confirmation_email(email, f"updates for {record_number}", confirm_url)
                    return jsonify({"message": "Confirmation email resent — check your inbox."}), 202
    except Exception as e:
        logger.error("[subscribe] matters insert error: %s", e)
        return jsonify({"error": "Database error"}), 500

    confirm_url = f"{BASE_URL}/subscribe/confirm/{confirm_token}"
    ok = _send_confirmation_email(email, f"updates for {record_number}", confirm_url)
    if not ok:
        logger.error("[subscribe] confirmation email failed for %s", email)
        return jsonify({"error": "Subscription saved but confirmation email failed. Please try again shortly."}), 500
    return jsonify({"message": "Check your email to confirm your subscription"}), 202


@bp.route("/subscribe/confirm/<token>")
def confirm_subscription(token: str):
    confirmed = False
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE meeting_subscriptions SET confirmed=TRUE WHERE confirm_token=%s",
                (token,),
            )
            if cur.rowcount:
                confirmed = True
            else:
                cur.execute(
                    "UPDATE matter_subscriptions SET confirmed=TRUE WHERE confirm_token=%s",
                    (token,),
                )
                if cur.rowcount:
                    confirmed = True
    except Exception as e:
        logger.error("[subscribe] confirm error: %s", e)

    if confirmed:
        page = _page("Subscription confirmed", "You're subscribed! You'll receive email updates soon.")
    else:
        page = _page("Link expired", "This confirmation link is invalid or has already been used.")
    resp = make_response(page, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@bp.route("/unsubscribe/<token>")
def unsubscribe(token: str):
    removed = False
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM meeting_subscriptions WHERE unsub_token=%s", (token,)
            )
            if cur.rowcount:
                removed = True
            else:
                cur.execute(
                    "DELETE FROM matter_subscriptions WHERE unsub_token=%s", (token,)
                )
                if cur.rowcount:
                    removed = True
    except Exception as e:
        logger.error("[subscribe] unsubscribe error: %s", e)

    if removed:
        page = _page("Unsubscribed", "You've been unsubscribed and won't receive further emails.")
    else:
        page = _page("Already removed", "This unsubscribe link is invalid or has already been used.")
    resp = make_response(page, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send_confirmation_email(to: str, description: str, confirm_url: str) -> bool:
    subject = "Confirm your subscription — The Government and Me"
    html_body = f"""<!DOCTYPE html><html><body style="font-family:Georgia,serif;max-width:520px;margin:40px auto;color:#222">
<h2 style="color:#1a3a5c">Confirm your subscription</h2>
<p>You asked to receive {description} from <strong>The Government and Me</strong>.</p>
<p><a href="{confirm_url}" style="background:#1a3a5c;color:#fff;padding:10px 20px;text-decoration:none;display:inline-block;border-radius:2px">Confirm subscription</a></p>
<p style="font-size:13px;color:#888">If you didn't request this, ignore this email.</p>
</body></html>"""
    return send_email(to, subject, html_body)


def _page(title: str, message: str) -> str:
    return f"""<!DOCTYPE html><html><head><title>{title}</title>
<style>body{{font-family:Georgia,serif;max-width:480px;margin:80px auto;color:#222;text-align:center}}
h2{{color:#1a3a5c}}a{{color:#1a6aad}}</style></head>
<body><h2>{title}</h2><p>{message}</p>
<p><a href="{BASE_URL}">← Back to The Government and Me</a></p></body></html>"""
