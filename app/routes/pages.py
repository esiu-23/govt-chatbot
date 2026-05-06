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
    return _no_cache(send_from_directory("static", "landing.html"))


@bp.route("/app")
def app_page():
    return _no_cache(send_from_directory("static", "index.html"))


@bp.route("/analyses")
def analyses_index():
    return redirect("/")


@bp.route("/analyses/who-controls-chicago")
def who_controls_chicago():
    return _no_cache(send_from_directory(
        os.path.join(_PROJECT_ROOT, "static", "analyses"),
        "who-controls-chicago.html"
    ))


@bp.route("/health")
def health():
    return jsonify({
        "status"       : "ok",
        "scrape_date"  : _rag.SCRAPE_DATE,
        "total_chunks" : _rag.TOTAL_CHUNKS,
        "model"        : MODEL_NAME,
    })
