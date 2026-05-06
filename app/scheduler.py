"""
APScheduler jobs for meeting email notifications and matter status polling.

Architecture
────────────
Two distinct jobs with distinct responsibilities:

1. sync_meeting_schedule()  — runs once a day at startup + every 24 h.
   Fetches ONLY the lightweight meeting list from ELMS (no agenda content).
   For each NEW meeting discovered:
     • Upserts into known_meetings with meeting_datetime
     • Schedules two one-shot DateTrigger polls: at meeting start time and
       3 h after (expected end)
     • If subscribers exist, fetches the full meeting record for
       publicCommentDeadline and sends a "new meeting scheduled" alert email
   For existing meetings: updates meeting_datetime if missing and
   re-registers DateTrigger polls (so they survive app restarts).

2. check_and_send_meeting_emails()  — safety-net, runs every 4 h AND is
   called by the DateTrigger polls at meeting start/end times.
   Queries known_meetings (never ELMS) for meetings needing agenda or
   summary emails, fetches+caches content from ELMS only for those meetings,
   and populates meeting_items + matter_detail_cache so page loads are DB-only.

Email types
───────────
  new_meeting  sent by sync_meeting_schedule on first discovery
  agenda       sent when a meeting with non-routine items is upcoming
  summary      sent after the meeting ends (≥3 h past start time)
"""

import logging
from datetime import datetime, timedelta, timezone

from concurrent.futures import ThreadPoolExecutor

from .data_sources.elms import (
    _COMMITTEE_CHAIRS,
    _attachment_summary,
    _elms_get,
    fetch_matter_detail_slim,
    fetch_meeting_items,
    get_enriched_matter,
    meeting_summary,
    plain_language_titles,
)
from .db import _db
from .email.sender import send_email
from .email.templates import (
    BASE_URL,
    render_agenda_email,
    render_matter_update_email,
    render_new_meeting_email,
    render_summary_email,
)

logger = logging.getLogger(__name__)

_SUBSCRIBABLE_BODIES = ["City Council"] + sorted(_COMMITTEE_CHAIRS.keys())

# ELMS status values that indicate the agenda has been officially published.
# "Scheduled & Published" is what the live ELMS API actually returns.
_PUBLISHED_STATUSES = {"published", "agenda published", "final agenda", "scheduled & published"}


# ---------------------------------------------------------------------------
# Public entry points (called by APScheduler)
# ---------------------------------------------------------------------------

def sync_meeting_schedule() -> None:
    """Daily lightweight sync: discover new meetings and schedule targeted content pulls.

    Fetches only the meeting list from ELMS (no agenda content).  For each NEW
    meeting not already in known_meetings:
      • Stores it with meeting_datetime
      • Schedules one-shot DateTrigger polls at start time and 3 h after
      • Sends a "new meeting scheduled" alert with the public comment deadline

    For ALL upcoming meetings (new and existing): re-registers DateTrigger polls
    so they survive app restarts.
    """
    logger.info("[scheduler] daily schedule sync starting")
    now       = datetime.now(timezone.utc)
    today_str = now.date().isoformat()
    end_str   = (now.date() + timedelta(days=90)).isoformat()

    try:
        raw = _elms_get("/meeting-agenda", {"top": 300, "orderby": "date asc"})
        meetings = raw.get("value", raw.get("data", []))
    except Exception as e:
        logger.error("[scheduler] sync ELMS fetch failed: %s", e)
        return

    past_30_str = (now.date() - timedelta(days=30)).isoformat()
    upcoming: list[dict] = []
    seen_ids: set[str] = set()
    for m in meetings:
        date_str = (m.get("date") or "")[:10]
        mid      = m.get("meetingId") or m.get("id")
        if date_str < past_30_str or date_str > end_str or not mid or not m.get("body"):
            continue
        if mid not in seen_ids:
            seen_ids.add(mid)
            upcoming.append(m)

    known_ids = _get_known_meeting_ids()
    scheduler  = _get_scheduler()
    new_count  = 0

    for m in upcoming:
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

        is_new = meeting_id not in known_ids

        # Upsert schedule metadata only — no content fetch here
        _upsert_meeting_state(meeting_id, body, date_str, elms_status, 0, meeting_datetime=meeting_dt)

        # Register DateTrigger polls for every upcoming meeting so they survive restarts
        if scheduler and meeting_dt and meeting_dt > now:
            _schedule_targeted_polls(scheduler, meeting_id, meeting_dt)

        if is_new:
            new_count += 1
            if _has_subscribers(body):
                try:
                    full_record = _elms_get(f"/meeting-agenda/{meeting_id}")
                except Exception:
                    full_record = {}
                items = _safe_fetch_items(meeting_id)
                _send_new_meeting_alert(meeting_id, body, date_str, meeting_dt, full_record, items)

    logger.info("[scheduler] sync complete — %d upcoming meetings, %d new", len(upcoming), new_count)


def check_and_send_meeting_emails() -> None:
    """Process meetings in known_meetings that need agenda or summary emails.

    Called by DateTrigger jobs at meeting start time and 3 h after, and by the
    4 h safety-net interval job.  Never fetches the ELMS schedule list — that
    is sync_meeting_schedule()'s job.  Fetches ELMS content only for the
    specific meetings that need it, and caches results so page loads are DB-only.
    """
    logger.info("[scheduler] check_and_send starting")
    now       = datetime.now(timezone.utc)
    today_str = now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()

    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT meeting_id, body, meeting_date, meeting_datetime, elms_status, location
                   FROM known_meetings
                   WHERE (
                     (agenda_sent_at  IS NULL AND meeting_date >= %s)
                     OR
                     (summary_sent_at IS NULL AND meeting_date BETWEEN %s AND %s)
                   )
                   AND EXISTS (
                     SELECT 1 FROM meeting_subscriptions ms
                     WHERE ms.body = known_meetings.body AND ms.confirmed = TRUE
                   )""",
                (today_str, yesterday, today_str),
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.error("[scheduler] check_and_send DB query: %s", e)
        return

    for meeting_id, body, meeting_date, meeting_dt, elms_status, location in rows:
        is_past = (meeting_dt < now) if meeting_dt else (meeting_date <= today_str)
        state   = _load_meeting_state(meeting_id) or {}
        elms_status = elms_status or ""
        location    = location or ""

        # ── Agenda ───────────────────────────────────────────────────────────
        if not state.get("agenda_sent_at") and not is_past:
            items       = _safe_fetch_items(meeting_id)
            non_routine = [i for i in items if not i.get("isRoutine")]
            if non_routine:
                _prewarm_matter_cache(non_routine)
                enriched      = _enrich_items_for_email(items)
                routine_count = sum(1 for i in items if i.get("isRoutine"))
                _dispatch_meeting_emails("agenda", meeting_id, body, meeting_date,
                                         enriched, routine_count, location, "")
                _mark_agenda_sent(meeting_id, body, meeting_date, elms_status,
                                  len(non_routine), routine_count, location)
                logger.info("[scheduler] agenda email sent: %s %s", body, meeting_date)

        # ── Summary ──────────────────────────────────────────────────────────
        if not state.get("summary_sent_at") and is_past:
            items         = _safe_fetch_items(meeting_id)
            non_routine   = [i for i in items if not i.get("isRoutine")]
            _prewarm_matter_cache(non_routine)
            summary_text  = meeting_summary(meeting_id, body, meeting_date, items)
            enriched      = _enrich_items_for_email(items)
            routine_count = sum(1 for i in items if i.get("isRoutine"))
            _dispatch_meeting_emails("summary", meeting_id, body, meeting_date,
                                     enriched, routine_count, "", summary_text)
            _mark_summary_sent(meeting_id, body, meeting_date, elms_status,
                               len(non_routine), routine_count, location)
            logger.info("[scheduler] summary email sent: %s %s", body, meeting_date)

        _touch_meeting_state(meeting_id, elms_status)

    _reschedule_meeting_job()


def check_and_send_matter_updates() -> None:
    """Poll matter status for all confirmed subscribers and email on changes."""
    logger.info("[scheduler] matter status check starting")
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, email, record_number, last_status, unsub_token "
                "FROM matter_subscriptions WHERE confirmed=TRUE"
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.error("[scheduler] matter_subscriptions fetch: %s", e)
        return

    for row_id, email_addr, record_number, last_status, unsub_token in rows:
        try:
            slim = fetch_matter_detail_slim(record_number)
        except Exception:
            continue

        new_status = slim.get("status") or ""
        if not new_status or new_status == last_status:
            if last_status is None and new_status:
                _update_last_status(row_id, new_status)
            continue

        plain_title = _get_plain_title(record_number)
        att_summary = _get_cached_attachment_summary(record_number)
        unsub_url   = f"{BASE_URL}/unsubscribe/{unsub_token}"

        subj, html = render_matter_update_email(
            record_number=record_number,
            plain_title=plain_title,
            old_status=last_status or "Unknown",
            new_status=new_status,
            change_date="",
            attachment_summary=att_summary,
            unsub_url=unsub_url,
        )
        send_email(email_addr, subj, html)
        _update_last_status(row_id, new_status)


# ---------------------------------------------------------------------------
# New-meeting alert helpers
# ---------------------------------------------------------------------------

def _send_new_meeting_alert(
    meeting_id: str,
    body: str,
    date_str: str,
    meeting_dt: "datetime | None",
    full_record: dict,
    items: list[dict],
) -> None:
    """Dispatch a 'new meeting scheduled' alert to confirmed subscribers."""
    location     = full_record.get("location") or ""
    deadline_iso = full_record.get("publicCommentDeadline") or ""
    deadline_str = _fmt_deadline(deadline_iso)

    meeting_time_str = ""
    if meeting_dt:
        try:
            from zoneinfo import ZoneInfo
            dt_ct = meeting_dt.astimezone(ZoneInfo("America/Chicago"))
            meeting_time_str = dt_ct.strftime("%-I:%M %p CT")
        except Exception:
            pass

    non_routine   = [i for i in items if not i.get("isRoutine")]
    routine_count = sum(1 for i in items if i.get("isRoutine"))
    if non_routine:
        pt = plain_language_titles([
            {"recordNumber": i.get("recordNumber"), "title": i.get("matterTitle")}
            for i in non_routine
        ])
        for item in non_routine:
            item["plainLanguageTitle"] = pt.get(item.get("recordNumber")) or item.get("matterTitle", "")

    for email_addr in _get_subscribers(body):
        if _already_sent(email_addr, meeting_id, "new_meeting"):
            continue
        unsub_url = _unsub_url_for(email_addr)
        subj, html_body = render_new_meeting_email(
            body, date_str, meeting_time_str, location,
            non_routine, routine_count, deadline_str, unsub_url,
        )
        if send_email(email_addr, subj, html_body):
            _log_sent(email_addr, meeting_id, "new_meeting")
    logger.info("[scheduler] new meeting alert sent: %s %s", body, date_str)


def _fmt_deadline(deadline_iso: str) -> str:
    """Format ELMS publicCommentDeadline ISO string to a Chicago-local display string."""
    if not deadline_iso:
        return ""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/Chicago")).strftime("%-m/%-d/%Y at %-I:%M %p CT")
    except Exception:
        return deadline_iso[:16].replace("T", " ") + " UTC"


# ---------------------------------------------------------------------------
# DateTrigger poll scheduling
# ---------------------------------------------------------------------------

def _schedule_targeted_polls(scheduler, meeting_id: str, meeting_dt: datetime) -> None:
    """Register one-shot content pulls at meeting start time and 3 h after.

    Using replace_existing=True means re-running on restart safely overwrites
    any previously registered jobs for the same meeting.
    """
    from apscheduler.triggers.date import DateTrigger
    now = datetime.now(timezone.utc)

    if meeting_dt > now:
        scheduler.add_job(
            check_and_send_meeting_emails,
            trigger=DateTrigger(run_date=meeting_dt),
            id=f"poll_start_{meeting_id}",
            replace_existing=True,
            max_instances=1,
        )

    post_dt = meeting_dt + timedelta(hours=3)
    if post_dt > now:
        scheduler.add_job(
            check_and_send_meeting_emails,
            trigger=DateTrigger(run_date=post_dt),
            id=f"poll_end_{meeting_id}",
            replace_existing=True,
            max_instances=1,
        )


# ---------------------------------------------------------------------------
# Matter cache pre-warming
# ---------------------------------------------------------------------------

def _prewarm_matter_cache(non_routine_items: list[dict]) -> None:
    """Fetch and cache full matter detail for every non-routine item in parallel."""
    record_numbers = [i["recordNumber"] for i in non_routine_items if i.get("recordNumber")]
    if not record_numbers:
        return

    def _fetch(rn):
        try:
            get_enriched_matter(rn)
        except Exception as e:
            logger.warning("[scheduler] prewarm failed for %s: %s", rn, e)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_fetch, record_numbers))

    logger.info("[scheduler] prewarmed matter_detail_cache for %d matters", len(record_numbers))


# ---------------------------------------------------------------------------
# Item enrichment for email matter cards
# ---------------------------------------------------------------------------

def _enrich_items_for_email(items: list[dict]) -> list[dict]:
    """Enrich non-routine items for email rendering.

    Reads matter fields from matter_detail_cache (populated by _prewarm_matter_cache)
    so this never hits ELMS — every matter was already fetched and cached before
    this function is called.
    """
    import json as _json
    non_routine = [i for i in items if not i.get("isRoutine", False)]
    pt = plain_language_titles(non_routine)
    for item in non_routine:
        rn = item.get("recordNumber")
        if not rn:
            continue
        item["plainLanguageTitle"] = pt.get(rn) or item.get("matterTitle", "")
        # Read from matter_detail_cache first (pre-warmed); fall back to slim ELMS call
        try:
            with _db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT data FROM matter_detail_cache WHERE record_number = %s", (rn,))
                row = cur.fetchone()
            if row:
                d = row[0] if isinstance(row[0], dict) else _json.loads(row[0])
                item.update({
                    "status":          d.get("status"),
                    "controllingBody": d.get("controllingBody"),
                    "introductionDate": d.get("introductionDate"),
                })
            else:
                slim = fetch_matter_detail_slim(rn)
                item.update({
                    "status":          slim.get("status"),
                    "controllingBody": slim.get("controllingBody"),
                    "introductionDate": slim.get("introductionDate"),
                })
        except Exception:
            pass
        _load_attachment_summary(item)
    return non_routine


def _load_attachment_summary(item: dict) -> None:
    rn = item.get("recordNumber")
    if not rn:
        return
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT summary FROM attachment_summaries WHERE url LIKE %s LIMIT 1",
                (f"%{rn}%",),
            )
            row = cur.fetchone()
            if row:
                item["attachmentSummary"] = row[0]
                return
    except Exception:
        pass
    try:
        detail = _elms_get(f"/matter/recordNumber/{rn}")
        attachments = [
            f for f in (detail.get("attachments") or [])
            if f.get("path") or f.get("url")
        ]
        if attachments:
            url   = attachments[0].get("path") or attachments[0].get("url")
            fname = attachments[0].get("fileName") or attachments[0].get("name") or ""
            summary = _attachment_summary(url, file_name=fname)
            if summary:
                item["attachmentSummary"] = summary
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------

def _safe_fetch_items(meeting_id: str) -> list[dict]:
    try:
        items = fetch_meeting_items(meeting_id)
        _cache_meeting_items(meeting_id, items)
        return items
    except Exception as e:
        logger.warning("[scheduler] fetch_meeting_items %s: %s", meeting_id, e)
        return []


def _cache_meeting_items(meeting_id: str, items: list[dict]) -> None:
    if not items:
        return
    try:
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
    except Exception as e:
        logger.warning("[scheduler] cache_meeting_items %s: %s", meeting_id, e)


def _dispatch_meeting_emails(
    email_type: str,
    meeting_id: str,
    body: str,
    date_str: str,
    enriched: list[dict],
    routine_count: int,
    location: str,
    summary_text: str,
) -> None:
    subscribers = _get_subscribers(body)
    for email_addr in subscribers:
        if _already_sent(email_addr, meeting_id, email_type):
            continue
        unsub_url = _unsub_url_for(email_addr)
        if email_type == "agenda":
            subj, html = render_agenda_email(body, date_str, location, enriched, routine_count, unsub_url)
        else:
            subj, html = render_summary_email(body, date_str, summary_text, enriched, routine_count, unsub_url)
        if send_email(email_addr, subj, html):
            _log_sent(email_addr, meeting_id, email_type)


# ---------------------------------------------------------------------------
# Adaptive rescheduling
# ---------------------------------------------------------------------------

def _reschedule_meeting_job() -> None:
    """
    Adjust the meeting-email job interval based on how soon the nearest
    upcoming meeting is.  Tighter polls as a meeting approaches.
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = _get_scheduler()
        if scheduler is None:
            return

        days_until = _days_until_next_subscribed_meeting()

        if days_until is None:
            hours = 6       # no upcoming meetings — idle pace
        elif days_until < 2:
            hours = None    # < 2 days out: poll every 20 minutes
            minutes = 20
        elif days_until < 7:
            hours = 1       # 2-7 days out: hourly
            minutes = None
        elif days_until < 14:
            hours = 2       # 1-2 weeks out: every 2 hours
            minutes = None
        else:
            hours = 6       # > 2 weeks out: every 6 hours
            minutes = None

        if hours is None:
            trigger = IntervalTrigger(minutes=minutes)
            interval_desc = f"{minutes}min"
        else:
            trigger = IntervalTrigger(hours=hours)
            interval_desc = f"{hours}h"

        scheduler.reschedule_job("meeting_emails", trigger=trigger)
        logger.info("[scheduler] rescheduled meeting_emails to every %s (next meeting in %s days)",
                    interval_desc, days_until)
    except Exception as e:
        logger.warning("[scheduler] reschedule failed (non-fatal): %s", e)


_scheduler_ref = None  # set by start_scheduler()

def _get_scheduler():
    return _scheduler_ref


def _days_until_next_subscribed_meeting() -> int | None:
    """Return days until the nearest relevant subscribed meeting.

    Returns 0 if a meeting from yesterday or today still awaits a summary
    (keeps the 20-min polling cadence active right after meetings end).
    """
    today = datetime.now(timezone.utc).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    today_str = today.isoformat()
    try:
        with _db() as conn:
            cur = conn.cursor()
            # If any recent meeting still needs a summary, poll frequently so
            # the email goes out the same day the meeting data becomes available.
            cur.execute(
                """SELECT 1 FROM known_meetings km
                   WHERE km.meeting_date BETWEEN %s AND %s
                     AND km.summary_sent_at IS NULL
                     AND EXISTS (
                       SELECT 1 FROM meeting_subscriptions ms
                       WHERE ms.body = km.body AND ms.confirmed = TRUE
                     )
                   LIMIT 1""",
                (yesterday, today_str),
            )
            if cur.fetchone():
                return 0  # < 2-day branch → 20-min polling

            # Normal: next upcoming meeting
            cur.execute(
                """SELECT km.meeting_date
                   FROM known_meetings km
                   WHERE km.meeting_date > %s
                     AND km.agenda_sent_at IS NULL
                     AND EXISTS (
                       SELECT 1 FROM meeting_subscriptions ms
                       WHERE ms.body = km.body AND ms.confirmed = TRUE
                     )
                   ORDER BY km.meeting_date ASC
                   LIMIT 1""",
                (today_str,),
            )
            row = cur.fetchone()
            if row:
                next_date = datetime.strptime(row[0], "%Y-%m-%d").date()
                return (next_date - today).days
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# known_meetings DB helpers
# ---------------------------------------------------------------------------

def _load_meeting_state(meeting_id: str) -> dict | None:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT elms_status, nonroutine_count, agenda_sent_at, summary_sent_at "
                "FROM known_meetings WHERE meeting_id=%s",
                (meeting_id,),
            )
            row = cur.fetchone()
            if row:
                return {
                    "elms_status":       row[0],
                    "nonroutine_count":  row[1],
                    "agenda_sent_at":    row[2],
                    "summary_sent_at":   row[3],
                }
    except Exception:
        pass
    return None


def _get_known_meeting_ids() -> set[str]:
    """Return the set of all meeting_ids currently stored in known_meetings."""
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT meeting_id FROM known_meetings")
            return {row[0] for row in cur.fetchall()}
    except Exception:
        return set()


def _upsert_meeting_state(
    meeting_id: str, body: str, meeting_date: str, elms_status: str, nonroutine_count: int,
    routine_count: int = 0, location: str = "", meeting_datetime: "datetime | None" = None,
) -> dict:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO known_meetings
                       (meeting_id, body, meeting_date, meeting_datetime,
                        elms_status, nonroutine_count, routine_count, location)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (meeting_id) DO UPDATE
                     SET meeting_datetime = COALESCE(EXCLUDED.meeting_datetime, known_meetings.meeting_datetime),
                         elms_status      = EXCLUDED.elms_status,
                         last_checked_at  = NOW()""",
                (meeting_id, body, meeting_date, meeting_datetime,
                 elms_status, nonroutine_count, routine_count, location),
            )
    except Exception as e:
        logger.warning("[scheduler] upsert_meeting_state: %s", e)
    return {
        "elms_status": elms_status,
        "nonroutine_count": nonroutine_count,
        "agenda_sent_at": None,
        "summary_sent_at": None,
    }


def _touch_meeting_state(meeting_id: str, elms_status: str) -> None:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE known_meetings SET elms_status=%s, last_checked_at=NOW() "
                "WHERE meeting_id=%s",
                (elms_status, meeting_id),
            )
    except Exception as e:
        logger.warning("[scheduler] touch_meeting_state: %s", e)


def _mark_agenda_sent(
    meeting_id: str, body: str, meeting_date: str, elms_status: str,
    nonroutine_count: int, routine_count: int = 0, location: str = "",
) -> None:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO known_meetings
                       (meeting_id, body, meeting_date, elms_status, nonroutine_count, routine_count, location, agenda_sent_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (meeting_id) DO UPDATE
                     SET agenda_sent_at   = NOW(),
                         nonroutine_count = EXCLUDED.nonroutine_count,
                         routine_count    = EXCLUDED.routine_count,
                         location         = COALESCE(EXCLUDED.location, known_meetings.location),
                         elms_status      = EXCLUDED.elms_status,
                         last_checked_at  = NOW()""",
                (meeting_id, body, meeting_date, elms_status, nonroutine_count, routine_count, location),
            )
    except Exception as e:
        logger.warning("[scheduler] mark_agenda_sent: %s", e)


def _mark_summary_sent(
    meeting_id: str, body: str, meeting_date: str, elms_status: str,
    nonroutine_count: int, routine_count: int = 0, location: str = "",
) -> None:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO known_meetings
                       (meeting_id, body, meeting_date, elms_status, nonroutine_count, routine_count, location, summary_sent_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (meeting_id) DO UPDATE
                     SET summary_sent_at  = NOW(),
                         nonroutine_count = EXCLUDED.nonroutine_count,
                         routine_count    = EXCLUDED.routine_count,
                         location         = COALESCE(EXCLUDED.location, known_meetings.location),
                         elms_status      = EXCLUDED.elms_status,
                         last_checked_at  = NOW()""",
                (meeting_id, body, meeting_date, elms_status, nonroutine_count, routine_count, location),
            )
    except Exception as e:
        logger.warning("[scheduler] mark_summary_sent: %s", e)


# ---------------------------------------------------------------------------
# Subscription / email-log DB helpers
# ---------------------------------------------------------------------------

def _has_subscribers(body: str) -> bool:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM meeting_subscriptions WHERE body=%s AND confirmed=TRUE LIMIT 1",
                (body,),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _get_subscribers(body: str) -> list[str]:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT email FROM meeting_subscriptions WHERE body=%s AND confirmed=TRUE",
                (body,),
            )
            return [r[0] for r in cur.fetchall()]
    except Exception:
        return []


def _already_sent(email: str, meeting_id: str, email_type: str) -> bool:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM meeting_email_log "
                "WHERE email=%s AND meeting_id=%s AND email_type=%s",
                (email, meeting_id, email_type),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _log_sent(email: str, meeting_id: str, email_type: str) -> None:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO meeting_email_log (email, meeting_id, email_type) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (email, meeting_id, email_type),
            )
    except Exception as e:
        logger.warning("[scheduler] log_sent: %s", e)


def _unsub_url_for(email: str) -> str:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT unsub_token FROM meeting_subscriptions WHERE email=%s LIMIT 1",
                (email,),
            )
            row = cur.fetchone()
            if row:
                return f"{BASE_URL}/unsubscribe/{row[0]}"
    except Exception:
        pass
    return f"{BASE_URL}/unsubscribe/invalid"


def _update_last_status(row_id: int, status: str) -> None:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE matter_subscriptions SET last_status=%s WHERE id=%s",
                (status, row_id),
            )
    except Exception as e:
        logger.warning("[scheduler] update_last_status: %s", e)


def _get_plain_title(record_number: str) -> str:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT plain_title FROM plain_language_titles WHERE record_number=%s",
                (record_number,),
            )
            row = cur.fetchone()
            if row:
                return row[0]
    except Exception:
        pass
    return record_number


def _get_cached_attachment_summary(record_number: str) -> str | None:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT summary FROM attachment_summaries WHERE url LIKE %s LIMIT 1",
                (f"%{record_number}%",),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Scheduler registration
# ---------------------------------------------------------------------------

def start_scheduler(app) -> None:
    """Register and start APScheduler background jobs."""
    global _scheduler_ref
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = BackgroundScheduler(daemon=True)
        _scheduler_ref = scheduler

        # Daily schedule sync — runs immediately at startup so DateTrigger polls
        # for all upcoming meetings are re-registered after restarts.
        scheduler.add_job(
            sync_meeting_schedule,
            trigger=IntervalTrigger(hours=24),
            id="sync_meetings",
            replace_existing=True,
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )

        # Safety-net fallback: catches anything the DateTrigger polls miss
        # (e.g., past meetings whose post-poll fired before the app was running).
        scheduler.add_job(
            check_and_send_meeting_emails,
            trigger=IntervalTrigger(hours=4),
            id="meeting_emails",
            replace_existing=True,
            max_instances=1,
        )

        scheduler.add_job(
            check_and_send_matter_updates,
            trigger=IntervalTrigger(hours=4),
            id="matter_updates",
            replace_existing=True,
            max_instances=1,
        )
        scheduler.start()
        logger.info("[scheduler] started — sync_meetings at startup + every 24h; "
                    "meeting_emails every 4h (fallback); matter_updates every 4h")
    except Exception as e:
        logger.error("[scheduler] failed to start: %s", e)
