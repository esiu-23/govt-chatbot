#!/usr/bin/env python3
"""
One-time migration: add legislativeTracker + sort actions for every row in
matter_detail_cache.

No ELMS calls, no AI calls — pure DB read / transform / write.

Run from project root (with DATABASE_URL set):
    python backfill_tracker.py
"""

import json
import os
import sys
import socket
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s: %(message)s")


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
            print(f"Resolved {hostname} → {params['hostaddr']}", flush=True)
        except Exception as e:
            print(f"IPv4 resolution warning: {e}", flush=True)

    db_module._pool = psycopg2.pool.ThreadedConnectionPool(1, 4, **params)
    print("DB pool ready", flush=True)


def main() -> None:
    _init_db()

    from app.db import _db
    from app.data_sources.elms import _build_legislative_tracker

    # Load all cached matters
    with _db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT record_number, data FROM matter_detail_cache")
        rows = cur.fetchall()

    print(f"Found {len(rows)} cached matters to backfill", flush=True)

    updated = errors = skipped = 0

    for record_number, raw in rows:
        try:
            matter = raw if isinstance(raw, dict) else json.loads(raw)

            # Sort actions chronologically
            actions = matter.get("actions") or []
            actions.sort(key=lambda a: (a.get("actionDate") or ""))
            matter["actions"] = actions

            # Build and attach tracker
            matter["legislativeTracker"] = _build_legislative_tracker(matter)

            with _db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE matter_detail_cache SET data = %s WHERE record_number = %s",
                    (json.dumps(matter), record_number),
                )

            updated += 1
            if updated % 50 == 0:
                print(f"  {updated}/{len(rows)} updated…", flush=True)

        except Exception as e:
            errors += 1
            print(f"  ERROR {record_number}: {e}", flush=True)

    print(f"\nDone — {updated} updated, {skipped} skipped, {errors} errors", flush=True)


if __name__ == "__main__":
    main()
