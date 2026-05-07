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

Phase 2 — Matters with an action date in 2026:
  Collect all record numbers from 2026 meeting agendas (meeting_items join
  known_meetings). This catches matters introduced in any year that appeared
  on a 2026 committee or council agenda. Enrich and cache each uncached one.

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

    # Always generate a summary — for no-item meetings this fetches the Agenda
    # PDF, extracts matter IDs, and calls link_agenda_matters automatically.
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

def _active_2026_matter_rns() -> set[str]:
    """Collect record numbers from published 2026 meeting agendas (meeting_items table)."""
    from app.db import _db

    print("Collecting record numbers from published 2026 meeting agendas…", flush=True)
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT DISTINCT mi.record_number
                   FROM meeting_items mi
                   JOIN known_meetings km USING (meeting_id)
                   WHERE km.meeting_date >= %s
                     AND km.meeting_date <= %s
                     AND mi.record_number IS NOT NULL""",
                (YEAR_START, TODAY),
            )
            rns = {row[0] for row in cur.fetchall()}
    except Exception as e:
        logger.warning("Could not read meeting_items: %s", e)
        rns = set()

    print(f"  → {len(rns)} record numbers from published agendas", flush=True)
    return rns


def _empty_agenda_meetings() -> list[tuple]:
    """Return (meeting_id, body, meeting_date) for 2026 meetings with no items in meeting_items."""
    from app.db import _db
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT km.meeting_id, km.body, km.meeting_date
                   FROM known_meetings km
                   WHERE km.meeting_date >= %s
                     AND km.meeting_date <= %s
                     AND km.elms_status != 'Cancelled'
                     AND NOT EXISTS (
                       SELECT 1 FROM meeting_items mi WHERE mi.meeting_id = km.meeting_id
                     )
                   ORDER BY km.meeting_date""",
                (YEAR_START, TODAY),
            )
            return cur.fetchall()
    except Exception as e:
        logger.warning("Could not query empty-agenda meetings: %s", e)
        return []


def _elms_search_for_committee(body: str, top: int = 200) -> list[dict]:
    """Search ELMS for matters associated with a committee body."""
    from app.data_sources.elms import _elms_get
    try:
        raw = _elms_get("/search", {"search": body, "top": top})
        return raw.get("value", raw.get("data", []))
    except Exception as e:
        logger.warning("ELMS search failed for %r: %s", body, e)
        return []


def _rns_for_empty_agenda_meeting(body: str, date_str: str) -> set[str]:
    """
    Search ELMS for matters acted on by `body` within ±1 day of `date_str`.
    Returns record numbers of matching matters.
    """
    from app.data_sources.elms import _elms_get
    from datetime import date, timedelta

    try:
        meeting_date = date.fromisoformat(date_str)
    except Exception:
        return set()

    window_start = (meeting_date - timedelta(days=1)).isoformat()
    window_end   = (meeting_date + timedelta(days=1)).isoformat()

    results = _elms_search_for_committee(body)
    rns: set[str] = set()
    for m in results:
        rn = m.get("recordNumber")
        if not rn:
            continue
        # Fetch full matter to inspect actions (search result doesn't include actions)
        try:
            full = _elms_get(f"/matter/recordNumber/{rn}")
        except Exception:
            continue
        for action in (full.get("actions") or []):
            action_date_raw = (action.get("actionDate") or "")[:10]
            action_by = action.get("actionByName") or ""
            if (window_start <= action_date_raw <= window_end
                    and body.lower() in action_by.lower()):
                rns.add(rn)
                break
        time.sleep(0.05)

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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", type=int, choices=[1, 2, 3], default=None,
                        help="Run only a specific phase (default: all)")
    args = parser.parse_args()
    run_all = args.phase is None

    _init_db()
    t0 = time.time()

    from app.data_sources.elms import get_enriched_matter, _classify_routine
    from app.db import _db as _get_db

    # ── Phase 1: Meetings ────────────────────────────────────────────────────
    p1_total = p1_items = p1_matters = p1_summaries = 0
    if run_all or args.phase == 1:
        print("\n" + "=" * 60, flush=True)
        print("PHASE 1 — 2026 meetings", flush=True)
        print("=" * 60, flush=True)

        meetings  = _fetch_2026_meetings()
        p1_total  = len(meetings)
        for i, m in enumerate(meetings, 1):
            body     = m.get("body") or ""
            date_str = (m.get("date") or "")[:10]
            print(f"\n[{i}/{p1_total}] {body} — {date_str}", flush=True)
            stats = _process_one_meeting(m)
            print(f"  items={stats['items']}  "
                  f"matters={stats['matters_ok']}+{stats['matters_err']}err  "
                  f"summary={'✓' if stats['summary'] else '–'}", flush=True)
            for err in stats["errors"]:
                print(f"  ⚠  {err}", flush=True)
            p1_items     += stats["items"]
            p1_matters   += stats["matters_ok"]
            p1_summaries += int(stats["summary"])
            time.sleep(0.15)

        print(f"\nPhase 1 done — {p1_total} meetings, {p1_items} items, "
              f"{p1_matters} matters cached, {p1_summaries} summaries", flush=True)

    # ── Phase 2: matters from published agendas (any introduction year) ──────
    p2_ok = p2_err = 0
    if run_all or args.phase == 2:
        print("\n" + "=" * 60, flush=True)
        print("PHASE 2 — matters on 2026 meeting agendas (any introduction year)", flush=True)
        print("=" * 60, flush=True)

        search_rns = _active_2026_matter_rns()
        cached_rns = _already_cached_rns()
        new_rns    = search_rns - cached_rns
        print(f"  {len(search_rns)} on 2026 agendas, {len(cached_rns)} already cached, "
              f"{len(new_rns)} to enrich", flush=True)

        def _enrich(rn):
            try:
                get_enriched_matter(rn)
                return True
            except Exception:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_enrich, rn): rn for rn in new_rns}
            for fut in as_completed(futures):
                if fut.result():
                    p2_ok += 1
                else:
                    p2_err += 1
                done = p2_ok + p2_err
                if done % 25 == 0 or done == len(new_rns):
                    print(f"  {done}/{len(new_rns)} enriched ({p2_err} errors)", flush=True)

        print(f"\nPhase 2 done — {p2_ok} matters enriched, {p2_err} errors", flush=True)

    # ── Phase 3: empty-agenda meetings — ELMS search by committee name ────────
    p3_meetings = p3_matters = p3_errors = 0
    if run_all or args.phase == 3:
        print("\n" + "=" * 60, flush=True)
        print("PHASE 3 — empty-agenda meetings (ELMS search by committee)", flush=True)
        print("=" * 60, flush=True)

        def _enrich_and_cache_item(meeting_id, body, date_str, rn):
            try:
                matter       = get_enriched_matter(rn)
                matter_type  = matter.get("type") or ""
                matter_title = matter.get("title") or matter.get("shortTitle") or ""
                action_name  = ""
                for a in (matter.get("actions") or []):
                    if body.lower() in (a.get("actionByName") or "").lower():
                        action_name = a.get("actionName") or ""
                        break
                is_routine = _classify_routine(matter_type, matter_title)
                with _get_db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        """INSERT INTO meeting_items
                               (meeting_id, record_number, matter_id, matter_title,
                                matter_type, action_name, is_routine, item_order)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, 0)
                           ON CONFLICT (meeting_id, record_number) DO UPDATE
                             SET matter_title = EXCLUDED.matter_title,
                                 matter_type  = EXCLUDED.matter_type,
                                 action_name  = EXCLUDED.action_name,
                                 is_routine   = EXCLUDED.is_routine,
                                 cached_at    = NOW()""",
                        (meeting_id, rn, matter.get("matterId"), matter_title,
                         matter_type, action_name, is_routine),
                    )
                return True
            except Exception as e:
                logger.warning("Phase 3 enrich failed %s / %s: %s", meeting_id, rn, e)
                return False

        empty_meetings = _empty_agenda_meetings()
        print(f"  {len(empty_meetings)} meetings with no published agenda items", flush=True)

        for meeting_id, body, meeting_date in empty_meetings:
            date_str = str(meeting_date)
            print(f"\n  {body} — {date_str}", flush=True)
            rns = _rns_for_empty_agenda_meeting(body, date_str)
            print(f"    {len(rns)} matters found via ELMS search", flush=True)
            if not rns:
                continue
            p3_meetings += 1
            for rn in rns:
                ok = _enrich_and_cache_item(meeting_id, body, date_str, rn)
                if ok:
                    p3_matters += 1
                else:
                    p3_errors += 1
            time.sleep(0.1)

        print(f"\nPhase 3 done — {p3_meetings} meetings recovered, "
              f"{p3_matters} matters enriched, {p3_errors} errors", flush=True)

    # ── Summary ──────────────────────────────────────────────────────────────
    elapsed = time.time() - t0
    print("\n" + "=" * 60, flush=True)
    print(f"ALL DONE in {elapsed / 60:.1f} min", flush=True)
    if run_all or args.phase == 1:
        print(f"  Meetings processed : {p1_total}", flush=True)
        print(f"  Agenda items cached: {p1_items}", flush=True)
        print(f"  Meeting summaries  : {p1_summaries}", flush=True)
    print(f"  Matters enriched   : {p1_matters + p2_ok + p3_matters}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
