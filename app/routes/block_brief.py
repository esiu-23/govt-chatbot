"""Block Brief — subscribe/confirm/unsubscribe and static services API."""

import logging
import os
import secrets
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, redirect, request
from psycopg2.extras import Json as PgJson

from ..db import _db
from ..email.sender import send_email
from ..routes.know_your_block import (
    _fetch_json, _geo, _geo_field, _get, _fetch_park_events,
    _NOMINATIM, _SSL_CTX, _TOKEN, _BASE,
)

logger = logging.getLogger(__name__)
bp = Blueprint("block_brief", __name__)

_BASE_URL = os.environ.get("BASE_URL", "https://thegovernmentandme.tools")

VALID_PREFS = {"safety", "food", "construction", "services", "business", "parks"}

# ── Geocode helper ───────────────────────────────────────────────────────────

def _geocode(address: str) -> tuple[float, float] | None:
    q = address.strip()
    if "chicago" not in q.lower():
        q += ", Chicago, IL"
    url = f"{_NOMINATIM}?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 1, "countrycodes": "us"}
    )
    data = _fetch_json(url, {"User-Agent": "TheGovernmentAndMe/1.0"})
    if not isinstance(data, list) or not data:
        return None
    r = data[0]
    return float(r["lat"]), float(r["lon"])


# ── Static services fetch (welcome email + page preview) ────────────────────

def _fetch_static_services(lat: float, lng: float, radius_m: int = 804) -> dict:
    geo = _geo(lat, lng, radius_m)
    cta_bus_where = _geo_field("the_geom", lat, lng, radius_m)

    queries = {
        "libraries": ("wa2i-tm5d", {
            "$where": geo,
            "$select": "name,address,hours_of_operation,phone,location",
            "$limit": "5",
        }),
        "cps_schools": ("wg9x-4ke6", {
            "$where": geo,
            "$select": "school_nm,grades,address,phone,location",
            "$limit": "10",
        }),
        "speed_cameras": ("4i42-qv3h", {
            "$where": geo,
            "$select": "address,first_approach,second_approach,location",
            "$limit": "10",
        }),
        "red_light_cameras": ("thvf-6diy", {
            "$where": geo,
            "$select": "intersection,first_approach,second_approach,location",
            "$limit": "10",
        }),
        "farmers_markets": ("atzs-u7pv", {
            "$where": geo,
            "$select": "market_name,location_description,days_hours,location",
            "$limit": "5",
        }),
        "parks": ("ejsh-fztr", {
            "$where": f"within_circle(the_geom, {lat}, {lng}, {radius_m})",
            "$select": "label,location,hours,the_geom",
            "$limit": "6",
        }),
    }

    results = {}
    with ThreadPoolExecutor(max_workers=7) as executor:
        futures = {
            executor.submit(_get, did, params): key
            for key, (did, params) in queries.items()
        }
        futures[executor.submit(_get, "hvnx-qtky", {
            "$where": cta_bus_where,
            "$select": "stop_name,street,cross_st,routesstpg,the_geom",
            "$limit": "10",
        })] = "cta_bus_stops"
        futures[executor.submit(_get, "8pix-ypme", {
            "$where": geo,
            "$select": "stop_name,station_name,station_descriptive_name,red,blue,g,brn,p,y,pnk,o,location",
            "$limit": "8",
        })] = "cta_l_stops"
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as e:
                logger.warning("[block_brief] static %s: %s", key, e)
                results[key] = []

    return results


# ── Weekly signals fetch (per subscribed categories) ────────────────────────

def _fetch_weekly_signals(lat: float, lng: float, radius_m: int, prefs: list[str]) -> dict:
    geo = _geo(lat, lng, radius_m)
    d7  = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    d30 = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")

    pref_set = set(prefs)
    queries = {}

    if "safety" in pref_set:
        queries["crimes"] = ("ijzp-q8t2", {
            "$where": f"{geo} AND date > '{d7}'",
            "$order": "date DESC",
            "$select": "date,primary_type,description,location_description,block,latitude,longitude",
            "$limit": "25",
        })
        queries["traffic_crashes"] = ("85ca-t3if", {
            "$where": f"{geo} AND crash_date > '{d7}'",
            "$order": "crash_date DESC",
            "$select": "crash_date,crash_type,injuries_total,most_severe_injury,street_name,latitude,longitude",
            "$limit": "15",
        })

    if "food" in pref_set:
        queries["food_inspections"] = ("4ijn-s7e5", {
            "$where": f"{geo} AND inspection_date > '{d7}'",
            "$order": "inspection_date DESC",
            "$select": "dba_name,results,inspection_date,address,latitude,longitude",
            "$limit": "20",
        })

    if "construction" in pref_set:
        queries["building_permits"] = ("ydr8-5enu", {
            "$where": f"{geo} AND application_start_date > '{d7}'",
            "$order": "application_start_date DESC",
            "$select": "permit_type,work_description,reported_cost,street_number,street_direction,street_name,application_start_date,contact_1_name,permit_status,latitude,longitude",
            "$limit": "20",
        })
        queries["building_violations"] = ("22u3-xenr", {
            "$where": f"{geo} AND violation_date > '{d7}'",
            "$order": "violation_date DESC",
            "$select": "violation_date,violation_description,inspection_status,address,latitude,longitude",
            "$limit": "15",
        })
        queries["street_closures"] = ("rzy5-8tax", {
            "$where": f"{geo} AND permit_start_date > '{d30}'",
            "$order": "permit_start_date DESC",
            "$select": "permit_type,street_number,street_name,from_street,to_street,permit_start_date,permit_end_date,latitude,longitude",
            "$limit": "10",
        })

    if "services" in pref_set:
        queries["sr_requests"] = ("v6vf-nfxy", {
            "$where": f"{geo} AND created_date > '{d7}'",
            "$order": "created_date DESC",
            "$select": "sr_number,sr_type,created_date,closed_date,status,street_address,ward,latitude,longitude",
            "$limit": "30",
        })
        queries["potholes_patched"] = ("wqdh-9gek", {
            "$where": f"{geo} AND completion_date > '{d7}'",
            "$order": "completion_date DESC",
            "$select": "address,completion_date,number_of_potholes_filled_on_block,latitude,longitude",
            "$limit": "10",
        })
        queries["streetlight_outages"] = ("5w22-e46m", {
            "$where": f"{geo} AND creation_date > '{d7}'",
            "$order": "creation_date DESC",
            "$select": "street_address,status,creation_date,completion_date,latitude,longitude",
            "$limit": "10",
        })

    if "business" in pref_set:
        queries["business_licenses"] = ("uupf-x98q", {
            "$where": f"{geo} AND date_issued > '{d7}'",
            "$order": "date_issued DESC",
            "$select": "legal_name,doing_business_as_name,license_description,address,date_issued,license_status,latitude,longitude",
            "$limit": "20",
        })
        queries["liquor_licenses"] = ("49ig-icpy", {
            "$where": f"{geo} AND date_issued > '{d7}'",
            "$order": "date_issued DESC",
            "$select": "legal_name,license_description,license_status,date_issued,address,latitude,longitude",
            "$limit": "15",
        })

    if "parks" in pref_set:
        queries["block_parties"] = ("9zhy-9n5f", {
            "$where": f"{geo} AND applicationstartdate > '{d7}'",
            "$order": "applicationstartdate DESC",
            "$select": "streetname,streetclosure,applicationstartdate,applicationenddate,latitude,longitude",
            "$limit": "10",
        })
        queries["farmers_markets"] = ("atzs-u7pv", {
            "$where": geo,
            "$select": "market_name,location_description,days_hours,location",
            "$limit": "5",
        })

    results = {}
    futures_map = {}
    with ThreadPoolExecutor(max_workers=max(len(queries) + 1, 2)) as executor:
        for key, (did, params) in queries.items():
            futures_map[executor.submit(_get, did, params)] = key
        if "parks" in pref_set:
            futures_map[executor.submit(_fetch_park_events, lat, lng, radius_m, d7)] = "park_events"
        for future in as_completed(futures_map):
            key = futures_map[future]
            try:
                results[key] = future.result()
            except Exception as e:
                logger.warning("[block_brief] weekly %s: %s", key, e)
                results[key] = []

    return results


# ── Routes ───────────────────────────────────────────────────────────────────

@bp.route("/api/block-brief/services")
def services():
    address = (request.args.get("address") or "").strip()
    if address:
        coords = _geocode(address)
        if not coords:
            return jsonify({"error": "Could not geocode address"}), 400
        lat, lng = coords
    else:
        try:
            lat = float(request.args["lat"])
            lng = float(request.args["lng"])
        except (KeyError, ValueError):
            return jsonify({"error": "lat, lng, or address required"}), 400
    radius_m = int(float(request.args.get("radius_mi", 0.5)) * 1609.34)
    data = _fetch_static_services(lat, lng, radius_m)
    return jsonify(data)


@bp.route("/block-brief/subscribe", methods=["POST"])
def subscribe():
    body = request.get_json(silent=True) or {}
    email   = (body.get("email") or "").strip().lower()
    address = (body.get("address") or "").strip()
    prefs   = [p for p in (body.get("preferences") or []) if p in VALID_PREFS]

    if not email or "@" not in email:
        return jsonify({"error": "Valid email required"}), 400
    if not address:
        return jsonify({"error": "Address required"}), 400

    coords = _geocode(address)
    if not coords:
        return jsonify({"error": "Could not geocode address. Try including the full street number and name."}), 400
    lat, lng = coords

    confirm_token   = secrets.token_urlsafe(32)
    unsub_token     = secrets.token_urlsafe(32)

    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO block_brief_subscriptions
                        (email, address, lat, lng, preferences, confirm_token, unsubscribe_token)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (email, address) DO UPDATE SET
                        preferences    = EXCLUDED.preferences,
                        confirm_token  = EXCLUDED.confirm_token,
                        confirmed      = FALSE,
                        last_sent_at   = NULL
                    RETURNING id
                """, (email, address, lat, lng, PgJson(prefs or list(VALID_PREFS)), confirm_token, unsub_token))
    except Exception as e:
        logger.error("[block_brief] subscribe DB error: %s", e)
        return jsonify({"error": "Could not save subscription"}), 500

    confirm_url = f"{_BASE_URL}/block-brief/confirm/{confirm_token}"
    _send_confirm_email(email, address, confirm_url)

    return jsonify({"ok": True, "message": "Check your email to confirm your subscription."})


@bp.route("/block-brief/confirm/<token>")
def confirm(token: str):
    if not token:
        return "Invalid link.", 400

    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE block_brief_subscriptions
                    SET confirmed = TRUE
                    WHERE confirm_token = %s AND confirmed = FALSE
                    RETURNING id, email, address, lat, lng, preferences, unsubscribe_token
                """, (token,))
                row = cur.fetchone()
    except Exception as e:
        logger.error("[block_brief] confirm DB error: %s", e)
        return "Something went wrong. Please try again.", 500

    if not row:
        return redirect("/?bb=already_confirmed")

    sub_id, email, address, lat, lng, prefs, unsub_token = row
    unsub_url = f"{_BASE_URL}/block-brief/unsubscribe/{unsub_token}"

    services = _fetch_static_services(lat, lng)
    html = _render_welcome_email(address, lat, lng, prefs, services, unsub_url)
    send_email(email, f"Welcome to your block — {address}", html)

    return redirect(f"/block-brief?confirmed=1&address={urllib.parse.quote(address)}")


@bp.route("/block-brief/unsubscribe/<token>")
def unsubscribe(token: str):
    if not token:
        return "Invalid link.", 400
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM block_brief_subscriptions WHERE unsubscribe_token = %s RETURNING email",
                    (token,)
                )
                row = cur.fetchone()
    except Exception as e:
        logger.error("[block_brief] unsubscribe DB error: %s", e)
        return "Something went wrong.", 500

    return redirect("/?bb=unsubscribed")


# ── Email sending helpers ────────────────────────────────────────────────────

def _send_confirm_email(email: str, address: str, confirm_url: str) -> None:
    from ..email.templates import render_block_brief_confirm
    html = render_block_brief_confirm(address, confirm_url)
    send_email(email, f"Confirm your Block Brief subscription — {address}", html)


def _render_welcome_email(address, lat, lng, prefs, services, unsub_url) -> str:
    from ..email.templates import render_block_brief_welcome
    return render_block_brief_welcome(address, lat, lng, prefs, services, unsub_url)


def send_weekly_block_briefs() -> None:
    """Called by scheduler every Monday. Fetches and sends weekly digests."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, email, address, lat, lng, radius_mi, preferences, unsubscribe_token
                    FROM block_brief_subscriptions
                    WHERE confirmed = TRUE
                      AND (last_sent_at IS NULL OR last_sent_at < %s)
                    ORDER BY id
                    LIMIT 500
                """, (cutoff,))
                subs = cur.fetchall()
    except Exception as e:
        logger.error("[block_brief] scheduler DB read error: %s", e)
        return

    logger.info("[block_brief] sending weekly digests to %d subscribers", len(subs))

    for row in subs:
        sub_id, email, address, lat, lng, radius_mi, prefs, unsub_token = row
        radius_m = int((radius_mi or 0.5) * 1609.34)
        try:
            signals = _fetch_weekly_signals(lat, lng, radius_m, prefs or list(VALID_PREFS))
            unsub_url = f"{_BASE_URL}/block-brief/unsubscribe/{unsub_token}"
            from ..email.templates import render_block_brief_weekly
            html = render_block_brief_weekly(address, prefs or list(VALID_PREFS), signals, unsub_url)
            sent = send_email(email, f"Your block brief — {datetime.now().strftime('%b %-d, %Y')} | {address}", html)
            if sent:
                with _db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE block_brief_subscriptions SET last_sent_at = NOW() WHERE id = %s",
                            (sub_id,)
                        )
        except Exception as e:
            logger.error("[block_brief] weekly send failed for sub %s: %s", sub_id, e)
