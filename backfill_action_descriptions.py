#!/usr/bin/env python3
"""
Backfill actionByName into legislativeTracker steps for all cached matters.

Reads every row in matter_detail_cache, re-runs _build_legislative_tracker
(which now includes actionByName), and writes the updated data back.
No ELMS API calls needed — works entirely from cached JSONB.

Run from project root:
    python backfill_action_descriptions.py
"""

import json
import os
import sys
import socket
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _init_db():
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

    db_module._pool = psycopg2.pool.ThreadedConnectionPool(1, 4, **params)
    print("DB pool ready", flush=True)


def main():
    _init_db()

    from app.db import _db
    from app.data_sources.elms import _build_legislative_tracker

    # Read all cached matters
    print("Reading matter_detail_cache…", flush=True)
    with _db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT record_number, data FROM matter_detail_cache")
        rows = cur.fetchall()

    print(f"  {len(rows)} matters to backfill", flush=True)

    updated = errors = skipped = 0
    t0 = time.time()

    for record_number, data in rows:
        matter = data if isinstance(data, dict) else json.loads(data)

        try:
            new_tracker = _build_legislative_tracker(matter)
        except Exception as e:
            print(f"  ⚠  {record_number}: build_tracker error: {e}", flush=True)
            errors += 1
            continue

        old_tracker = matter.get("legislativeTracker") or []

        # Check if any step is missing actionByName that the new tracker has
        def _needs_update(old, new):
            if len(old) != len(new):
                return True
            for o, n in zip(old, new):
                if o.get("actionByName") != n.get("actionByName"):
                    return True
            return False

        if not _needs_update(old_tracker, new_tracker):
            skipped += 1
            continue

        matter["legislativeTracker"] = new_tracker

        try:
            with _db() as conn:
                cur = conn.cursor()
                cur.execute(
                    """UPDATE matter_detail_cache
                       SET data = %s, cached_at = NOW()
                       WHERE record_number = %s""",
                    (json.dumps(matter), record_number),
                )
            updated += 1
        except Exception as e:
            print(f"  ⚠  {record_number}: DB write error: {e}", flush=True)
            errors += 1

        if (updated + errors) % 100 == 0:
            print(f"  {updated + errors + skipped}/{len(rows)} processed…", flush=True)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s — {updated} updated, {skipped} already current, {errors} errors", flush=True)


if __name__ == "__main__":
    main()
