"""
APScheduler jobs for meeting email notifications and matter status polling.

Meeting detection strategy
──────────────────────────
Rather than re-examining 300 meetings from scratch every 2 hours, we maintain a
`known_meetings` table that stores each meeting's last-seen state (ELMS status,
non-routine item count, whether emails have been sent).  Each poll only fetches
a narrow time window (past 7 days + next 30 days), diffs each meeting against
its stored state, and acts only on genuine transitions:

  • elms_status changes to a "published" keyword  → agenda email
  • nonroutine_count goes from 0 → N              → agenda email (fallback if
                                                    ELMS status isn't granular)
  • meeting_date crosses into the past             → summary email

After each run we inspect the nearest upcoming meeting and reschedule the job
at an adaptive interval so we check more often as a meeting approaches:

  Next meeting ≥ 14 days out  →  6-hour poll
  Next meeting  7-14 days out →  2-hour poll
  Next meeting  2-7 days out  →  1-hour poll
  Next meeting  < 2 days out  →  20-minute poll
  No upcoming meetings        →  6-hour poll (idle)
"""

import logging
from datetime import datetime, timedelta, timezone

from .data_sources.elms import (
    _COMMITTEE_CHAIRS,
    _attachment_summary,
    _elms_get,
    fetch_matter_detail_slim,
    fetch_meeting_items,
    meeting_summary,
    plain_language_titles,
)
from .db import _db
from .email.sender import send_email
from .email.templates import BASE_URL, render_agenda_email, render_matter_update_email, render_summary_email

logger = logging.getLogger(__name__)

_SUBSCRIBABLE_BODIES = ["City Council"] + sorted(_COMMITTEE_CHAIRS.keys())

# ELMS status values that indicate the agenda has been officially published.
# Logged empirically; add more values as they appear in practice.
_PUBLISHED_STATUSES = {"published", "agenda published", "final agenda"}


# ---------------------------------------------------------------------------
# Public entry points (called by APScheduler)
# ---------------------------------------------------------------------------

def check_and_send_meeting_emails() -> None:
    """
    Diff-based meeting poll.  Fetches only a rolling 37-day window, compares
    each meeting against `known_meetings`, and sends emails only on genuine
    state transitions.
    """
    logger.info("[scheduler] meeting email check starting")
    today = datetime.now(timezone.utc).date()
    window_start = (today - timedelta(days=7)).isoformat()
    window_end   = (today + timedelta(days=30)).isoformat()

    try:
        # Fetch only the window we care about.  ELMS supports simple date
        # string comparison via the search/orderby params; if the API gains
        # proper date filtering we can add it here.
        raw = _elms_get("/meeting-agenda", {"top": 300, "orderby": "date desc"})
        meetings = raw.get("value", raw.get("data", []))
    except Exception as e:
        logger.error("[scheduler] ELMS fetch failed: %s", e)
        return

    # Narrow to our window client-side (ELMS doesn't expose a date filter param).
    in_window = [
        m for m in meetings
        if window_start <= (m.get("date") or "")[:10] <= window_end
        and (m.get("meetingId") or m.get("id"))
        and m.get("body")
    ]

    # Deduplicate by (date, body) — ELMS sometimes returns the same meeting twice.
    seen_keys: set[tuple] = set()
    unique: list[dict] = []
    for m in in_window:
        key = ((m.get("date") or "")[:10], m.get("body", ""))
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(m)

    today_str = today.isoformat()
    for m in unique:
        meeting_id = m.get("meetingId") or m.get("id") or ""
        body       = m.get("body") or ""
        date_str   = (m.get("date") or "")[:10]
        elms_status = (m.get("status") or "").strip()
        is_past    = date_str <= today_str

        if not _has_subscribers(body):
            continue

        # Load or create the stored state row.
        state = _load_meeting_state(meeting_id)
        if state is None:
            state = _upsert_meeting_state(meeting_id, body, date_str, elms_status, 0)

        status_changed = elms_status != (state.get("elms_status") or "")

        # ── Agenda detection ──────────────────────────────────────────────
        if not state.get("agenda_sent_at") and not is_past:
            agenda_published = _detect_agenda_published(
                meeting_id, elms_status, state, status_changed
            )
            if agenda_published:
                items = _safe_fetch_items(meeting_id)
                non_routine = [i for i in items if not i.get("isRoutine", False)]
                if non_routine:
                    enriched     = _enrich_items_for_email(items)
                    routine_count = sum(1 for i in items if i.get("isRoutine", False))
                    _dispatch_meeting_emails(
                        "agenda", meeting_id, body, date_str,
                        enriched, routine_count,
                        location=m.get("location") or "",
                        summary_text="",
                    )
                    _mark_agenda_sent(meeting_id, body, date_str, elms_status, len(non_routine))
                    logger.info("[scheduler] agenda email sent for %s %s", body, date_str)

        # ── Summary detection ─────────────────────────────────────────────
        if not state.get("summary_sent_at") and is_past:
            items         = _safe_fetch_items(meeting_id)
            summary_text  = meeting_summary(meeting_id, body, date_str, items)
            enriched      = _enrich_items_for_email(items)
            routine_count = sum(1 for i in items if i.get("isRoutine", False))
            _dispatch_meeting_emails(
                "summary", meeting_id, body, date_str,
                enriched, routine_count,
                location="",
                summary_text=summary_text,
            )
            _mark_summary_sent(meeting_id, body, date_str, elms_status,
                               len([i for i in items if not i.get("isRoutine", False)]))
            logger.info("[scheduler] summary email sent for %s %s", body, date_str)

        # Always update last_checked_at and status snapshot.
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
# Agenda-published detection logic
# ---------------------------------------------------------------------------

def _detect_agenda_published(
    meeting_id: str,
    elms_status: str,
    state: dict,
    status_changed: bool,
) -> bool:
    """
    Return True if evidence suggests the agenda was just published.

    Two signals, in priority order:
    1. ELMS status transitioned to a known "published" keyword.
    2. Non-routine item count went from 0 → positive (agenda populated).

    We only act when there is a *transition* — not just current state — to
    avoid re-sending on every poll after an agenda is already known.
    """
    # Signal 1: explicit ELMS status transition
    if status_changed and elms_status.lower() in _PUBLISHED_STATUSES:
        logger.debug("[scheduler] %s: ELMS status → '%s' (published)", meeting_id, elms_status)
        return True

    # Signal 2: item count transition 0 → N
    prev_count = state.get("nonroutine_count") or 0
    if prev_count == 0:
        items = _safe_fetch_items(meeting_id)
        current_count = sum(1 for i in items if not i.get("isRoutine", False))
        if current_count > 0:
            logger.debug("[scheduler] %s: item count 0 → %d (agenda populated)", meeting_id, current_count)
            return True

    return False


# ---------------------------------------------------------------------------
# Item enrichment for email matter cards
# ---------------------------------------------------------------------------

def _enrich_items_for_email(items: list[dict]) -> list[dict]:
    non_routine = [i for i in items if not i.get("isRoutine", False)]
    pt = plain_language_titles(non_routine)
    for item in non_routine:
        rn = item.get("recordNumber")
        if not rn:
            continue
        item["plainLanguageTitle"] = pt.get(rn) or item.get("matterTitle", "")
        try:
            slim = fetch_matter_detail_slim(rn)
            item.update({
                "status":         slim.get("status"),
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
        return fetch_meeting_items(meeting_id)
    except Exception as e:
        logger.warning("[scheduler] fetch_meeting_items %s: %s", meeting_id, e)
        return []


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
    """Return days until the nearest upcoming meeting for a subscribed body, or None."""
    today = datetime.now(timezone.utc).date()
    try:
        with _db() as conn:
            cur = conn.cursor()
            # Find the next upcoming meeting across all bodies that have subscribers
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
                (today.isoformat(),),
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


def _upsert_meeting_state(
    meeting_id: str, body: str, meeting_date: str, elms_status: str, nonroutine_count: int
) -> dict:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO known_meetings
                       (meeting_id, body, meeting_date, elms_status, nonroutine_count)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (meeting_id) DO NOTHING""",
                (meeting_id, body, meeting_date, elms_status, nonroutine_count),
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
    meeting_id: str, body: str, meeting_date: str, elms_status: str, nonroutine_count: int
) -> None:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO known_meetings
                       (meeting_id, body, meeting_date, elms_status, nonroutine_count, agenda_sent_at)
                   VALUES (%s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (meeting_id) DO UPDATE
                     SET agenda_sent_at = NOW(),
                         nonroutine_count = EXCLUDED.nonroutine_count,
                         elms_status = EXCLUDED.elms_status,
                         last_checked_at = NOW()""",
                (meeting_id, body, meeting_date, elms_status, nonroutine_count),
            )
    except Exception as e:
        logger.warning("[scheduler] mark_agenda_sent: %s", e)


def _mark_summary_sent(
    meeting_id: str, body: str, meeting_date: str, elms_status: str, nonroutine_count: int
) -> None:
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO known_meetings
                       (meeting_id, body, meeting_date, elms_status, nonroutine_count, summary_sent_at)
                   VALUES (%s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (meeting_id) DO UPDATE
                     SET summary_sent_at = NOW(),
                         nonroutine_count = EXCLUDED.nonroutine_count,
                         elms_status = EXCLUDED.elms_status,
                         last_checked_at = NOW()""",
                (meeting_id, body, meeting_date, elms_status, nonroutine_count),
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

        # Start meeting polls at 2-hour cadence; first run will self-adjust via
        # _reschedule_meeting_job() based on the actual upcoming meeting calendar.
        scheduler.add_job(
            check_and_send_meeting_emails,
            trigger=IntervalTrigger(hours=2),
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
        logger.info("[scheduler] started — meeting_emails every 2h (adaptive), matter_updates every 4h")
    except Exception as e:
        logger.error("[scheduler] failed to start: %s", e)
