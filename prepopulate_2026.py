#!/usr/bin/env python3
"""
One-time pre-population of DB caches for all 2026 Chicago city council
meetings and matters.

Phase 1 — Meetings occurred in 2026:
  For each past meeting in 2026:
    • Upsert into known_meetings
    • Fetch and cache agenda items into meeting_items
    • Generate and cache a meeting summary into meeting_summaries
    • Enrich and cache every non-routine matter into matter_detail_cache,
      plain_language_titles, and attachment_summaries

Phase 2 — Matters introduced/active in 2026:
  Search ELMS for matters with introductionDate in 2026 that weren't
  already enriched during Phase 1. Enrich and cache each one.

Run from project root (with DATABASE_URL and ANTHROPIC_API_KEY set):
    python prepopulate_2026.py
"""

import os
import sys
import time
import logging
import socket
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("prepopulate")

YEAR_START = "2026-01-01"
TODAY      = datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# DB pool bootstrap (mirrors app/db.py without Flask)
# ---------------------------------------------------------------------------

def _init_db() -> None:
    import psycopg2
    import psycopg2.pool
    import app.db as db_module
    from app.config import DATABASE_URL

    if not DATABASE_URL:
        sys.exit("ERROR: DATABASE_URL env var not set")

    params = psycopg2.extensions.parse_dsn(DATABASE_URL)
    hostname = params.get("host", "")
    if hostname:
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
            params["hostaddr"] = infos[0][4][0]
            print(f"Resolved {hostname} → {params['hostaddr']} (IPv4)", flush=True)
        except Exception as e:
            print(f"IPv4 resolution warning: {e}", flush=True)

    db_module._pool = psycopg2.pool.ThreadedConnectionPool(1, 16, **params)
    print("DB pool ready", flush=True)


# ---------------------------------------------------------------------------
# Phase 1 helpers
# ---------------------------------------------------------------------------

def _fetch_2026_meetings() -> list[dict]:
    """Return all past meetings in 2026 from ELMS, deduped by meeting_id."""
    from app.data_sources.elms import _elms_get

    meetings: list[dict] = []
    seen: set[str] = set()
    skip = 0
    top  = 500

    print(f"Fetching meeting list from ELMS (date {YEAR_START} – {TODAY})…", flush=True)

    while True:
        raw  = _elms_get("/meeting-agenda", {"top": top, "skip": skip, "orderby": "date desc"})
        page = raw.get("value", raw.get("data", []))
        if not page:
            break

        stop = False
        for m in page:
            date_str = (m.get("date") or "")[:10]
            mid      = m.get("meetingId") or m.get("id")
            if not mid or not m.get("body"):
                continue
            if date_str > TODAY:
                continue                      # future meeting — not yet occurred
            if date_str < YEAR_START:
                stop = True
                break                         # past 2026 window — done
            if mid not in seen:
                seen.add(mid)
                meetings.append(m)

        if stop or len(page) < top:
            break
        skip += top
        time.sleep(0.5)

    print(f"  → {len(meetings)} past meetings in 2026", flush=True)
    return meetings


def _upsert_known_meeting(m: dict) -> None:
    from app.db import _db

    meeting_id  = m.get("meetingId") or m.get("id") or ""
    body        = m.get("body") or ""
    full_date   = m.get("date") or ""
    date_str    = full_date[:10]
    elms_status = (m.get("status") or "").strip()

    meeting_dt: datetime | None = None
    if full_date:
        try:
            meeting_dt = datetime.fromisoformat(full_date.replace("Z", "+00:00"))
        except Exception:
            pass

    with _db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO known_meetings
                   (meeting_id, body, meeting_date, meeting_datetime, elms_status)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (meeting_id) DO UPDATE
                 SET meeting_datetime = COALESCE(EXCLUDED.meeting_datetime, known_meetings.meeting_datetime),
                     elms_status      = EXCLUDED.elms_status,
                     last_checked_at  = NOW()""",
            (meeting_id, body, date_str, meeting_dt, elms_status),
        )


def _cache_items(meeting_id: str, items: list[dict]) -> None:
    """Write agenda items to meeting_items table."""
    from app.db import _db

    if not items:
        return
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


def _process_one_meeting(m: dict) -> dict:
    """
    Full pipeline for a single meeting:
      1. Upsert known_meetings
      2. Fetch + cache agenda items
      3. Generate + cache meeting summary
      4. Enrich all non-routine matters (parallel, 4 workers)
    Returns a stats dict.
    """
    from app.data_sources.elms import fetch_meeting_items, meeting_summary, get_enriched_matter

    meeting_id = m.get("meetingId") or m.get("id") or ""
    body       = m.get("body") or ""
    date_str   = (m.get("date") or "")[:10]
    stats      = {"items": 0, "matters_ok": 0, "matters_err": 0,
                  "summary": False, "errors": []}

    try:
        _upsert_known_meeting(m)
    except Exception as e:
        stats["errors"].append(f"known_meetings: {e}")

    try:
        items = fetch_meeting_items(meeting_id)
        _cache_items(meeting_id, items)
        stats["items"] = len(items)
    except Exception as e:
        stats["errors"].append(f"fetch_items: {e}")
        items = []

    if items:
        try:
            summary = meeting_summary(meeting_id, body, date_str, items)
            stats["summary"] = bool(summary)
        except Exception as e:
            stats["errors"].append(f"meeting_summary: {e}")

    non_routine = [i for i in items if not i.get("isRoutine") and i.get("recordNumber")]

    def _enrich(item):
        try:
            get_enriched_matter(item["recordNumber"])
            return True
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        for ok in pool.map(_enrich, non_routine):
            if ok:
                stats["matters_ok"] += 1
            else:
                stats["matters_err"] += 1

    return stats


# ---------------------------------------------------------------------------
# Phase 2 helpers
# ---------------------------------------------------------------------------

def _search_2026_matter_rns() -> set[str]:
    """
    Paginate ELMS /search ordered by introductionDate desc, collecting
    record numbers for matters introduced in 2026.
    Stops when introductionDate drops below 2026-01-01.
    """
    from app.data_sources.elms import _elms_get

    rns: set[str] = set()
    skip = 0
    top  = 50

    print(f"Searching ELMS for matters with introductionDate {YEAR_START}–{TODAY}…", flush=True)

    while True:
        try:
            raw = _elms_get("/search", {
                "search": "", "top": top, "skip": skip,
                "orderby": "introductionDate desc",
            })
        except Exception as e:
            logger.warning("search page skip=%d failed: %s", skip, e)
            break

        page = raw.get("value", raw.get("data", []))
        if not page:
            break

        stop = False
        for m in page:
            intro = (m.get("introductionDate") or "")[:10]
            rn    = m.get("recordNumber")
            if not rn:
                continue
            if intro > TODAY:
                continue
            if intro < YEAR_START:
                stop = True
                break
            rns.add(rn)

        if stop or len(page) < top:
            break
        skip += top
        time.sleep(0.3)

        if skip % 200 == 0:
            print(f"  …{len(rns)} record numbers collected (skip={skip})", flush=True)

    print(f"  → {len(rns)} matter record numbers from search", flush=True)
    return rns


def _already_cached_rns() -> set[str]:
    from app.db import _db
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT record_number FROM matter_detail_cache")
            return {row[0] for row in cur.fetchall()}
    except Exception as e:
        logger.warning("Could not read matter_detail_cache: %s", e)
        return set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _init_db()
    t0 = time.time()

    # ── Phase 1: Meetings ────────────────────────────────────────────────────
    print("\n" + "=" * 60, flush=True)
    print("PHASE 1 — 2026 meetings", flush=True)
    print("=" * 60, flush=True)

    meetings = _fetch_2026_meetings()
    total    = len(meetings)
    p1_items = p1_matters = p1_summaries = 0

    for i, m in enumerate(meetings, 1):
        meeting_id = m.get("meetingId") or m.get("id")
        body       = m.get("body") or ""
        date_str   = (m.get("date") or "")[:10]
        print(f"\n[{i}/{total}] {body} — {date_str}", flush=True)

        stats = _process_one_meeting(m)
        print(
            f"  items={stats['items']}  "
            f"matters={stats['matters_ok']}+{stats['matters_err']}err  "
            f"summary={'✓' if stats['summary'] else '–'}",
            flush=True,
        )
        for err in stats["errors"]:
            print(f"  ⚠  {err}", flush=True)

        p1_items    += stats["items"]
        p1_matters  += stats["matters_ok"]
        p1_summaries += int(stats["summary"])

        time.sleep(0.15)   # be polite between meetings

    print(f"\nPhase 1 done — {total} meetings, {p1_items} items, "
          f"{p1_matters} matters cached, {p1_summaries} summaries", flush=True)

    # ── Phase 2: Matters introduced in 2026 not yet cached ──────────────────
    print("\n" + "=" * 60, flush=True)
    print("PHASE 2 — 2026 matters (search)", flush=True)
    print("=" * 60, flush=True)

    search_rns = _search_2026_matter_rns()
    cached_rns = _already_cached_rns()
    new_rns    = search_rns - cached_rns
    print(f"  {len(search_rns)} found, {len(cached_rns)} already cached, "
          f"{len(new_rns)} to enrich", flush=True)

    from app.data_sources.elms import get_enriched_matter

    def _enrich(rn):
        try:
            get_enriched_matter(rn)
            return True
        except Exception:
            return False

    ok_count = err_count = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_enrich, rn): rn for rn in new_rns}
        for fut in as_completed(futures):
            if fut.result():
                ok_count += 1
            else:
                err_count += 1
            done = ok_count + err_count
            if done % 25 == 0 or done == len(new_rns):
                print(f"  {done}/{len(new_rns)} enriched ({err_count} errors)", flush=True)

    print(f"\nPhase 2 done — {ok_count} matters enriched, {err_count} errors", flush=True)

    # ── Summary ──────────────────────────────────────────────────────────────
    elapsed = time.time() - t0
    print("\n" + "=" * 60, flush=True)
    print(f"ALL DONE in {elapsed / 60:.1f} min", flush=True)
    print(f"  Meetings processed : {total}", flush=True)
    print(f"  Agenda items cached: {p1_items}", flush=True)
    print(f"  Meeting summaries  : {p1_summaries}", flush=True)
    print(f"  Matters enriched   : {p1_matters + ok_count}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
