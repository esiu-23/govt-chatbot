import os
from flask import Blueprint, jsonify, send_from_directory, redirect

from ..data_sources import rag as _rag
from ..config import MODEL_NAME

bp = Blueprint("pages", __name__)

# Project root is one level above this package
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/")
def index():
    # return _no_cache(send_from_directory("static", "landing.html"))
    return _no_cache(send_from_directory(
        os.path.join(_PROJECT_ROOT, "static"),
        "landing.html"
    ))


@bp.route("/app")
def app_page():
    # return _no_cache(send_from_directory("static", "index.html"))
    return _no_cache(send_from_directory(
        os.path.join(_PROJECT_ROOT, "static"),
        "index.html"
    ))


@bp.route("/know-your-block")
def know_your_block_page():
    return _no_cache(send_from_directory(
        os.path.join(_PROJECT_ROOT, "static"),
        "know-your-block.html"
    ))


@bp.route("/block-brief")
def block_brief_page():
    return _no_cache(send_from_directory(
        os.path.join(_PROJECT_ROOT, "static"),
        "block-brief.html"
    ))


@bp.route("/neighborhood-map")
def neighborhood_map():
    return _no_cache(send_from_directory(
        os.path.join(_PROJECT_ROOT, "static", "neighborhood-map"),
        "index.html"
    ))


@bp.route("/neighborhood-map/<path:filename>")
def neighborhood_map_assets(filename):
    return send_from_directory(
        os.path.join(_PROJECT_ROOT, "static", "neighborhood-map"),
        filename
    )


@bp.route("/analyses")
def analyses_index():
    return redirect("/")


@bp.route("/analyses/who-controls-chicago")
def who_controls_chicago():
    return _no_cache(send_from_directory(
        os.path.join(_PROJECT_ROOT, "static", "analyses"),
        "who-controls-chicago.html"
    ))


@bp.route("/analyses/jurisdiction-domain-map")
def jurisdiction_domain_map():
    return _no_cache(send_from_directory(
        os.path.join(_PROJECT_ROOT, "static", "analyses"),
        "jurisdiction-domain-map.html"
    ))


@bp.route("/health")
def health():
    return jsonify({
        "status"       : "ok",
        "scrape_date"  : _rag.SCRAPE_DATE,
        "total_chunks" : _rag.TOTAL_CHUNKS,
        "model"        : MODEL_NAME,
    })
