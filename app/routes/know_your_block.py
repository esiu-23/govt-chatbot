import json
import logging
import math
import os
import ssl
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import certifi
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
bp = Blueprint("know_your_block", __name__)

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_BASE = "https://data.cityofchicago.org/resource"
_COOK_BASE = "https://datacatalog.cookcountyil.gov/resource"
_TOKEN = os.environ.get("SOCRATA_APP_TOKEN", "")
_NOMINATIM = "https://nominatim.openstreetmap.org/search"

# Mirrors pin_lookup.ts from neighborhood-map repo
_DIRECTIONS: dict[str, str] = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "N": "N", "S": "S", "E": "E", "W": "W",
}
_STREET_TYPE_ABBR: dict[str, str] = {
    "AVENUE": "AVE", "STREET": "ST", "DRIVE": "DR", "BOULEVARD": "BLVD",
    "ROAD": "RD", "PLACE": "PL", "COURT": "CT", "LANE": "LN",
    "PARKWAY": "PKWY", "TERRACE": "TER", "CIRCLE": "CIR",
    "HIGHWAY": "HWY", "EXPRESSWAY": "EXPY",
}
_ALL_STREET_TOKENS = {*_STREET_TYPE_ABBR.keys(), *_STREET_TYPE_ABBR.values()}


def _fetch_json(url: str, headers: dict, timeout: int = 10) -> list | dict | None:
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.warning("[kyb] fetch %s: %s", url[:80], e)
        return None


def _parse_address(raw: str) -> tuple[str, str | None, str] | None:
    """Return (num, dir, street_name) for a Cook County LIKE pattern, or None."""
    tokens = raw.strip().upper().split(",")[0].replace(".", "").split()
    tokens = [t for t in tokens if t]
    if len(tokens) < 2 or not tokens[0].isdigit():
        return None
    num = tokens[0]
    i = 1
    dir_ = _DIRECTIONS.get(tokens[i])
    if dir_:
        i += 1
    rest = tokens[i:]
    if not rest:
        return None
    # Cook County LIKE patterns match better without a trailing street-type suffix
    if len(rest) > 1 and rest[-1] in _ALL_STREET_TOKENS:
        rest = rest[:-1]
    street = " ".join(_STREET_TYPE_ABBR.get(t, t) for t in rest)
    return num, dir_, street


def _get(dataset_id: str, params: dict) -> list:
    if _TOKEN:
        params = {**params, "$$app_token": _TOKEN}
    url = f"{_BASE}/{dataset_id}.json?" + urllib.parse.urlencode(params)
    data = _fetch_json(url, {"Accept": "application/json"}, timeout=15)
    if data is None:
        return []
    return data if isinstance(data, list) else []


def _geo(lat, lng, radius_m):
    return f"within_circle(location, {lat}, {lng}, {radius_m})"


def _geo_field(field: str, lat, lng, radius_m) -> str:
    return f"within_circle({field}, {lat}, {lng}, {radius_m})"


def _geo_bbox(lat: float, lng: float, radius_m: int) -> str:
    """Bounding-box filter for datasets with no Socrata geo field (lat/lng columns only).
    Use _filter_by_distance() on the result set to trim corners to a true circle."""
    dlat = radius_m / 111_000
    dlng = radius_m / (111_000 * math.cos(math.radians(lat)))
    return (
        f"latitude > '{lat - dlat}' AND latitude < '{lat + dlat}'"
        f" AND longitude > '{lng - dlng}' AND longitude < '{lng + dlng}'"
    )


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in meters between two lat/lng points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((phi2 - phi1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _filter_by_distance(records: list, lat: float, lng: float, radius_m: int) -> list:
    """Keep only records whose lat/lng falls within radius_m of (lat, lng)."""
    out = []
    for r in records:
        try:
            rlat = float(r.get("latitude") or "nan")
            rlng = float(r.get("longitude") or "nan")
            if _haversine_m(lat, lng, rlat, rlng) <= radius_m:
                out.append(r)
        except (ValueError, TypeError):
            pass
    return out


def _centroid_from_geom(geom: dict) -> tuple[float, float] | None:
    """Return (lat, lng) centroid from a Socrata GeoJSON geometry object."""
    try:
        gtype = geom.get("type", "")
        rings = geom.get("coordinates", [])
        if gtype == "Point" and rings:
            return float(rings[1]), float(rings[0])
        if gtype == "Polygon" and rings:
            pts = rings[0]
        elif gtype == "MultiPolygon" and rings:
            pts = rings[0][0]
        else:
            return None
        lngs = [c[0] for c in pts if len(c) >= 2]
        lats = [c[1] for c in pts if len(c) >= 2]
        if not lats:
            return None
        return sum(lats) / len(lats), sum(lngs) / len(lngs)
    except Exception:
        return None


def _fetch_park_events(lat: float, lng: float, radius_m: int, d_past: str) -> list:
    """Two-step: find CPD parks within radius, then fetch event permits for those parks."""
    token = {"$$app_token": _TOKEN} if _TOKEN else {}

    # Step 1 — find parks within radius (ejsh-fztr uses the_geom polygon geometry)
    parks_url = f"{_BASE}/ejsh-fztr.json?" + urllib.parse.urlencode({
        **token,
        "$where": f"within_circle(the_geom, {lat}, {lng}, {radius_m})",
        "$select": "park_no,label,location,the_geom",
        "$limit": "25",
    })
    parks = _fetch_json(parks_url, {"Accept": "application/json"}, timeout=15)
    if not isinstance(parks, list) or not parks:
        return []

    park_coords: dict[str, tuple[float, float]] = {}
    park_names: dict[str, str] = {}
    park_addrs: dict[str, str] = {}
    for p in parks:
        raw = p.get("park_no") or ""
        try:
            num = str(int(float(raw)))
        except (ValueError, TypeError):
            continue
        park_names[num] = p.get("label") or ""
        park_addrs[num] = p.get("location") or ""
        geom = p.get("the_geom")
        if isinstance(geom, dict):
            centroid = _centroid_from_geom(geom)
            if centroid:
                park_coords[num] = centroid

    if not park_coords:
        return []

    # Step 2 — event permits for those parks (pk66-w54g)
    park_in = ",".join(f"'{n}'" for n in park_coords)
    permits_url = f"{_BASE}/pk66-w54g.json?" + urllib.parse.urlencode({
        **token,
        "$where": f"park_number IN ({park_in}) AND reservation_end_date >= '{d_past}'",
        "$order": "reservation_start_date ASC",
        "$select": "park_number,park_facility_name,reservation_start_date,reservation_end_date,event_type,event_description,permit_status,organization",
        "$limit": "20",
    })
    permits = _fetch_json(permits_url, {"Accept": "application/json"}, timeout=15)
    if not isinstance(permits, list):
        return []

    for perm in permits:
        num = str(perm.get("park_number") or "").strip()
        if num in park_coords:
            clat, clng = park_coords[num]
            perm["latitude"] = str(clat)
            perm["longitude"] = str(clng)
        if num in park_names:
            perm["park_name"] = park_names[num]
        if num in park_addrs:
            perm["park_address"] = park_addrs[num]

    return permits


@bp.route("/api/address-complete")
def address_complete():
    q = (request.args.get("q") or "").strip()
    if len(q) < 4:
        return jsonify([])
    parsed = _parse_address(q)
    if not parsed:
        return jsonify([])
    num, dir_, street = parsed
    # Escape single quotes to prevent SoQL injection
    street_safe = street.replace("'", "''")
    patterns = []
    if dir_:
        patterns.append(f"{num} {dir_} {street_safe}%")
    patterns.append(f"{num} %{street_safe}%")

    for pattern in patterns:
        params = {
            "$where": f"prop_address_city_name='CHICAGO' AND upper(prop_address_full) like '{pattern}'",
            "$select": "distinct prop_address_full",
            "$limit": "10",
            "$order": "prop_address_full ASC",
        }
        url = f"{_COOK_BASE}/3723-97qp.json?" + urllib.parse.urlencode(params)
        data = _fetch_json(url, {"Accept": "application/json"})
        if isinstance(data, list) and data:
            return jsonify([r["prop_address_full"] for r in data if r.get("prop_address_full")])

    return jsonify([])


@bp.route("/api/geocode")
def geocode():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "q is required"}), 400
    if "chicago" not in q.lower():
        q += ", Chicago, IL"
    url = f"{_NOMINATIM}?" + urllib.parse.urlencode({
        "q": q, "format": "json", "limit": 1, "countrycodes": "us",
    })
    data = _fetch_json(url, {"User-Agent": "TheGovernmentAndMe/1.0"})
    if data is None:
        return jsonify({"error": "Geocoding failed"}), 500
    if not data:
        return jsonify({"error": "Address not found"}), 404
    r = data[0]
    return jsonify({"lat": float(r["lat"]), "lng": float(r["lon"]), "display": r["display_name"]})


@bp.route("/api/know-your-block")
def know_your_block():
    try:
        lat = float(request.args["lat"])
        lng = float(request.args["lng"])
        radius_mi = float(request.args.get("radius_mi", 0.5))
    except (KeyError, ValueError):
        return jsonify({"error": "lat and lng are required"}), 400

    radius_m = int(radius_mi * 1609.34)
    geo = _geo(lat, lng, radius_m)
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    d365 = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S")
    d90  = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S")
    d30  = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")

    queries = {
        "business_licenses": ("uupf-x98q", {
            "$where": f"{geo} AND (date_issued > '{d90}' OR date_issued IS NULL)",
            "$order": "date_issued DESC NULLS FIRST",
            "$select": "legal_name,doing_business_as_name,license_description,business_activity,address,ward,community_area_name,date_issued,license_status,latitude,longitude",
            "$limit": "25",
        }),
        "building_permits": ("ydr8-5enu", {
            "$where": f"{geo} AND (permit_type IN ('PERMIT - NEW CONSTRUCTION','PERMIT - WRECKING/DEMOLITION','PERMIT - RENOVATION/ALTERATION') OR permit_type LIKE '%EXPRESS PERMIT%') AND application_start_date > '{d90}'",
            "$order": "application_start_date DESC",
            "$select": "permit_type,work_description,reported_cost,street_number,street_direction,street_name,application_start_date,contact_1_name,latitude,longitude",
            "$limit": "25",
        }),
        "building_violations": ("22u3-xenr", {
            "$where": f"{geo} AND violation_date > '{d90}'",
            "$order": "violation_date DESC",
            "$select": "violation_date,violation_description,inspection_status,address,latitude,longitude",
            "$limit": "20",
        }),
        "food_inspections": ("4ijn-s7e5", {
            "$where": f"{geo} AND inspection_date > '{d90}'",
            "$order": "inspection_date DESC",
            "$select": "dba_name,results,inspection_date,address,latitude,longitude",
            "$limit": "20",
        }),
        "traffic_crashes": ("85ca-t3if", {
            "$where": f"{geo} AND crash_date > '{d90}'",
            "$order": "crash_date DESC",
            "$select": "crash_date,crash_type,injuries_total,most_severe_injury,street_name,latitude,longitude",
            "$limit": "25",
        }),
        "crimes": ("ijzp-q8t2", {
            "$where": f"{geo} AND date > '{d30}'",
            "$order": "date DESC",
            "$select": "date,primary_type,description,location_description,latitude,longitude",
            "$limit": "25",
        }),
        "block_parties": ("9zhy-9n5f", {
            "$where": f"{geo} AND applicationenddate >= '{now}'",
            "$order": "applicationstartdate ASC",
            "$select": "streetname,streetclosure,applicationstartdate,applicationenddate,latitude,longitude",
            "$limit": "10",
        }),
        "tif_projects": ("mex4-ppfc", {
            "$where": f"{geo} AND cdc_date > '{d365}'",
            "$order": "cdc_date DESC",
            "$select": "project_name,developer,approved_amount,tif_district,cdc_date,address,community_area,tif_subsidy_percentage,latitude,longitude",
            "$limit": "10",
        }),
        "potholes_patched": ("wqdh-9gek", {
            "$where": f"{geo} AND completion_date > '{d90}'",
            "$order": "completion_date DESC",
            "$select": "address,completion_date,number_of_potholes_filled_on_block,latitude,longitude",
            "$limit": "15",
        }),
        "sr_requests": ("v6vf-nfxy", {
            "$where": f"{geo} AND created_date > '{d90}'",
            "$order": "created_date DESC",
            "$select": "sr_number,sr_type,created_date,closed_date,status,street_address,ward,community_area,latitude,longitude",
            "$limit": "30",
        }),
        "liquor_licenses": ("49ig-icpy", {
            "$where": f"{geo} AND date_issued > '{d90}'",
            "$order": "date_issued DESC",
            "$select": "legal_name,license_description,license_status,date_issued,address,latitude,longitude",
            "$limit": "20",
        }),
        "street_closures": ("rzy5-8tax", {
            "$where": f"{geo} AND permit_start_date > '{d90}'",
            "$order": "permit_start_date DESC",
            "$select": "permit_type,street_number,street_name,from_street,to_street,permit_start_date,permit_end_date,latitude,longitude",
            "$limit": "15",
        }),
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
        "streetlight_outages": ("5w22-e46m", {
            "$where": f"{geo} AND creation_date > '{d30}'",
            "$order": "creation_date DESC",
            "$select": "street_address,status,creation_date,completion_date,latitude,longitude",
            "$limit": "15",
        }),
        "tree_trims": ("snkf-esnw", {
            "$where": f"{_geo_bbox(lat, lng, radius_m)} AND creation_date > '{d90}'",
            "$order": "creation_date DESC",
            "$select": "address,status,creation_date,completion_date,latitude,longitude",
            "$limit": "10",
        }),
        "farmers_markets": ("atzs-u7pv", {
            "$where": geo,
            "$select": "market_name,location_description,days_hours,location",
            "$limit": "5",
        }),
    }

    # CTA bus stops use the_geom (not location) — add separately
    cta_bus_where = _geo_field("the_geom", lat, lng, radius_m)
    cta_l_where = geo  # 8pix-ypme uses location (Socrata Location type)

    results = {}
    with ThreadPoolExecutor(max_workers=24) as executor:
        futures = {
            executor.submit(_get, did, params): key
            for key, (did, params) in queries.items()
        }
        futures[executor.submit(_fetch_park_events, lat, lng, radius_m, now)] = "park_events"
        futures[executor.submit(_get, "hvnx-qtky", {
            "$where": cta_bus_where,
            "$select": "stop_name,street,cross_st,routesstpg,the_geom",
            "$limit": "15",
        })] = "cta_bus_stops"
        futures[executor.submit(_get, "8pix-ypme", {
            "$where": cta_l_where,
            "$select": "stop_name,station_name,station_descriptive_name,red,blue,g,brn,p,y,pnk,o,location",
            "$limit": "10",
        })] = "cta_l_stops"
        # Point-in-polygon: which TIF district does this address fall inside?
        futures[executor.submit(_get, "fz5x-7zak", {
            "$where": f"intersects(the_geom, 'POINT ({lng} {lat})')",
            "$select": "tifname,tif_number,status,start_year,end_year",
            "$limit": "3",
        })] = "tif_district"
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as e:
                logger.warning("[kyb] %s failed: %s", key, e)
                results[key] = []

    # Post-filter bbox-fetched datasets to the actual circle radius
    for key in ("tree_trims",):
        if results.get(key):
            results[key] = _filter_by_distance(results[key], lat, lng, radius_m)

    return jsonify({
        "lat": lat,
        "lng": lng,
        "radius_mi": radius_mi,
        "signals": results,
    })
