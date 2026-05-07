#!/usr/bin/env python3
"""
One-time backfill: regenerate meeting summaries for meetings that still have
the generic placeholder text but now have linked matters in meeting_items.

Targets only rows where:
  meeting_summaries.summary = <standard placeholder>
  AND at least one non-routine matter exists in meeting_items for that meeting

Run from project root:
    DATABASE_URL=... ANTHROPIC_API_KEY=... python backfill_no_matter_summaries.py
"""

import os
import sys
import socket
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

STANDARD_TEXT = (
    "No votes were taken at this meeting. Instead, a subject matter hearing took place. "
    "Click into this meeting to see the meeting agenda."
)


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
        except Exception:
            pass

    db_module._pool = psycopg2.pool.ThreadedConnectionPool(1, 8, **params)
    print("DB pool ready", flush=True)


def _find_target_meetings() -> list[tuple]:
    """Return (meeting_id, body, meeting_date) for meetings with the standard
    placeholder text that also have at least one matter in meeting_items."""
    from app.db import _db
    with _db() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT ms.meeting_id, km.body, km.meeting_date
               FROM meeting_summaries ms
               JOIN known_meetings km USING (meeting_id)
               WHERE ms.summary = %s
                 AND EXISTS (
                   SELECT 1 FROM meeting_items mi
                   WHERE mi.meeting_id = ms.meeting_id
                 )
               ORDER BY km.meeting_date""",
            (STANDARD_TEXT,),
        )
        return cur.fetchall()


def main() -> None:
    _init_db()

    from app.data_sources.elms import meeting_summary

    targets = _find_target_meetings()
    print(f"Found {len(targets)} meetings to reprocess", flush=True)

    ok = err = skipped = 0
    for meeting_id, body, meeting_date in targets:
        date_str = str(meeting_date)
        print(f"  {body} — {date_str} ({meeting_id})", flush=True, end="  ")
        try:
            # Pass empty items list — meeting_summary will fall through the
            # standard-text cache check and generate from meeting_items.
            summary = meeting_summary(meeting_id, body, date_str, [])
            if summary and summary != STANDARD_TEXT:
                print(f"✓  {summary[:80]}…", flush=True)
                ok += 1
            else:
                print("– still no summary (no non-routine linked matters?)", flush=True)
                skipped += 1
        except Exception as e:
            print(f"ERROR: {e}", flush=True)
            err += 1

    print(f"\nDone — {ok} updated, {skipped} skipped, {err} errors", flush=True)


if __name__ == "__main__":
    main()
