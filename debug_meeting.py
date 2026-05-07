#!/usr/bin/env python3
"""
Debug script: inspect a specific meeting's agenda in ELMS and our DB cache.

Usage:
    python debug_meeting.py "pedestrian" 2026-04-07
"""
import os
import sys
import socket
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

ELMS_BASE = "https://api.chicityclerkelms.chicago.gov"
DATABASE_URL = os.environ.get("DATABASE_URL", "")

body_fragment = sys.argv[1] if len(sys.argv) > 1 else "pedestrian"
date_filter   = sys.argv[2] if len(sys.argv) > 2 else "2026-04-07"


def _init_db():
    import psycopg2
    import psycopg2.pool
    params = psycopg2.extensions.parse_dsn(DATABASE_URL)
    hostname = params.get("host", "")
    if hostname:
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
            params["hostaddr"] = infos[0][4][0]
        except Exception:
            pass
    return psycopg2.pool.ThreadedConnectionPool(1, 2, **params)


def elms_get(path, params=None):
    r = requests.get(ELMS_BASE + path, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


pool = _init_db()
conn = pool.getconn()
cur  = conn.cursor()

# 1. Find meeting in known_meetings
print(f"\n=== known_meetings: body ILIKE '%{body_fragment}%' AND meeting_date = '{date_filter}' ===")
cur.execute(
    "SELECT meeting_id, body, meeting_date, elms_status FROM known_meetings "
    "WHERE body ILIKE %s AND meeting_date = %s",
    (f"%{body_fragment}%", date_filter),
)
rows = cur.fetchall()
if not rows:
    print("  NOT FOUND in known_meetings")
    sys.exit(1)

for meeting_id, body, meeting_date, status in rows:
    print(f"  meeting_id={meeting_id}  body={body}  date={meeting_date}  status={status}")

meeting_id = rows[0][0]

# 2. Check meeting_items cache
print(f"\n=== meeting_items cache for {meeting_id} ===")
cur.execute(
    "SELECT record_number, matter_title, matter_type, is_routine, cached_at "
    "FROM meeting_items WHERE meeting_id = %s ORDER BY item_order",
    (meeting_id,),
)
cached_items = cur.fetchall()
if not cached_items:
    print("  EMPTY — no rows in meeting_items for this meeting_id")
else:
    print(f"  {len(cached_items)} cached items:")
    for rn, title, mtype, is_routine, cached_at in cached_items:
        print(f"    [{rn}] {mtype} | routine={is_routine} | cached={cached_at} | {(title or '')[:80]}")

# 3. Hit ELMS live
print(f"\n=== ELMS /meeting-agenda/{meeting_id} (live) ===")
try:
    detail = elms_get(f"/meeting-agenda/{meeting_id}")
except Exception as e:
    print(f"  ELMS ERROR: {e}")
    sys.exit(1)

agenda = detail.get("agenda") or {}
groups = agenda.get("groups", [])
print(f"  Top-level keys: {list(detail.keys())}")
print(f"  agenda keys: {list(agenda.keys()) if agenda else '(none)'}")
print(f"  groups count: {len(groups)}")

all_items = []
for i, g in enumerate(groups):
    group_items = g.get("items") or []
    print(f"  group[{i}] name={g.get('name', '(none)')!r}  items={len(group_items)}")
    for item in group_items:
        print(f"    keys: {list(item.keys())}")
        print(f"    recordNumber={item.get('recordNumber')!r}  matterType={item.get('matterType')!r}")
        print(f"    title={str(item.get('matterTitle') or item.get('title') or '')[:80]!r}")
        all_items.append(item)

if not groups:
    # Maybe items are directly on the agenda or detail
    direct_items = detail.get("items") or agenda.get("items") or []
    print(f"  No groups — direct items at detail['items']: {len(direct_items)}")
    for item in direct_items[:5]:
        print(f"    keys: {list(item.keys())}")
        print(f"    recordNumber={item.get('recordNumber')!r}")

print(f"\n  Total items found: {len(all_items)}")
items_with_rn = [i for i in all_items if i.get("recordNumber")]
items_without_rn = [i for i in all_items if not i.get("recordNumber")]
print(f"  Items WITH recordNumber: {len(items_with_rn)}")
print(f"  Items WITHOUT recordNumber: {len(items_without_rn)}")

pool.putconn(conn)
