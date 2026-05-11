"""HTML email render functions for meeting and matter notifications."""

import html
from datetime import datetime

APP_URL = "https://thegovernmentandme.tools/app"
BASE_URL = "https://thegovernmentandme.tools"

_CSS = """
  body { font-family: Georgia, serif; background: #f5f5f0; margin: 0; padding: 0; }
  .wrap { max-width: 620px; margin: 32px auto; background: #fff; border: 1px solid #ddd; }
  .hdr { background: #1a3a5c; color: #fff; padding: 24px 28px 20px; }
  .hdr-label { font-size: 11px; letter-spacing: 2px; text-transform: uppercase; color: #8ab0cc; margin: 0 0 4px; }
  .hdr-title { font-size: 20px; font-weight: bold; margin: 0; }
  .hdr-date { font-size: 13px; color: #8ab0cc; margin: 4px 0 0; }
  .body { padding: 24px 28px; }
  .section-label { font-size: 11px; letter-spacing: 2px; text-transform: uppercase;
                   color: #888; border-bottom: 1px solid #e5e5e5; padding-bottom: 6px; margin: 0 0 16px; }
  .overview { background: #f0f4f8; border-left: 3px solid #1a3a5c; padding: 14px 16px;
              font-size: 15px; line-height: 1.6; color: #222; margin: 0 0 28px; }
  .meta-row { font-size: 13px; color: #555; margin: 0 0 6px; }
  .meta-row strong { color: #222; }
  .count-note { font-size: 13px; color: #666; margin: 0 0 20px; }
  .matter { border: 1px solid #e5e5e5; padding: 16px; margin: 0 0 14px; border-radius: 2px; }
  .matter-head { font-size: 12px; color: #666; margin: 0 0 5px; letter-spacing: 0.3px; }
  .matter-title { font-size: 15px; font-weight: bold; color: #1a3a5c; margin: 0 0 4px; line-height: 1.4; }
  .matter-meta { font-size: 12px; color: #888; margin: 0 0 10px; }
  .matter-summary { font-size: 14px; color: #444; line-height: 1.6; margin: 0 0 12px;
                    border-top: 1px solid #f0f0f0; padding-top: 10px; }
  .matter-link { font-size: 13px; }
  .matter-link a { color: #1a6aad; text-decoration: none; }
  .more-link { text-align: center; padding: 12px 0 4px; }
  .more-link a { font-size: 13px; color: #1a6aad; }
  .status-change { background: #fff8e1; border-left: 3px solid #f9a825; padding: 14px 16px; margin: 0 0 20px; }
  .status-change .label { font-size: 11px; letter-spacing: 1px; text-transform: uppercase; color: #888; }
  .status-change .row { font-size: 14px; margin: 4px 0 0; color: #333; }
  .footer { background: #f9f9f9; border-top: 1px solid #e5e5e5; padding: 16px 28px;
            font-size: 12px; color: #888; text-align: center; }
  .footer a { color: #888; }
"""


def _h(s: str) -> str:
    return html.escape(str(s or ""), quote=False)


def _fmt_date(iso: str) -> str:
    """'2025-05-07T10:00:00' → 'May 7, 2025'"""
    try:
        dt = datetime.fromisoformat(iso[:10])
        return dt.strftime("%B %-d, %Y")
    except Exception:
        return iso[:10] if iso else ""


def _matter_card_html(item: dict, show_status: bool = True) -> str:
    rn = _h(item.get("recordNumber", ""))
    title = _h(item.get("plainLanguageTitle") or item.get("matterTitle") or rn)
    mtype = _h((item.get("matterType") or item.get("type") or "").title())
    status = _h(item.get("status") or "")
    controlling = _h(item.get("controllingBody") or "")
    intro = item.get("introductionDate") or ""
    summary = _h(item.get("attachmentSummary") or "")
    matter_url = f"{APP_URL}?matter={html.escape(item.get('recordNumber',''), quote=True)}"

    head_parts = [p for p in [rn, mtype, status if show_status else ""] if p]
    head = "  |  ".join(head_parts)

    meta_parts = [p for p in [controlling, f"Introduced {_fmt_date(intro)}" if intro else ""] if p]
    meta = "  ·  ".join(meta_parts)

    summary_html = f'<p class="matter-summary">{summary}</p>' if summary else ""

    return f"""
<div class="matter">
  <div class="matter-head">{head}</div>
  <div class="matter-title">{title}</div>
  {'<div class="matter-meta">' + meta + '</div>' if meta else ''}
  {summary_html}
  <div class="matter-link">→ <a href="{matter_url}">View details at thegovernmentandme.tools</a></div>
</div>"""


def _shell(header_label: str, header_title: str, header_date: str, body_html: str,
           unsub_url: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <p class="hdr-label">{_h(header_label)}</p>
    <h1 class="hdr-title">{_h(header_title)}</h1>
    <p class="hdr-date">{_h(header_date)}</p>
  </div>
  <div class="body">
    {body_html}
  </div>
  <div class="footer">
    You're receiving this because you subscribed to updates at
    <a href="{BASE_URL}">thegovernmentandme.tools</a>.<br>
    <a href="{_h(unsub_url)}">Unsubscribe</a>
  </div>
</div>
</body></html>"""


def _meeting_link_html(meeting_id: str, label: str = "View full meeting details") -> str:
    """Return an HTML anchor linking to the meeting deep-link in the app."""
    if not meeting_id:
        return f'<a href="{APP_URL}#legislation" style="color:#1a6aad;text-decoration:none;">→ {_h(label)}</a>'
    url = f"{APP_URL}?meeting={html.escape(meeting_id, quote=True)}#legislation"
    return f'<a href="{url}" style="color:#1a6aad;text-decoration:none;">→ {_h(label)}</a>'


def _meeting_docs_html(meeting_docs: list[dict]) -> str:
    """Return an HTML block listing meeting-level document attachments."""
    if not meeting_docs:
        return ""
    rows = ""
    for doc in meeting_docs:
        name = _h(doc.get("name") or "Meeting document")
        url  = html.escape(doc.get("url") or "", quote=True)
        if url:
            rows += f'<div style="margin:6px 0"><a href="{url}" style="color:#1a6aad;text-decoration:none;">↗ {name}</a></div>\n'
        else:
            rows += f'<div style="margin:6px 0;color:#444">{name}</div>\n'
    return f"""
<p class="section-label">Meeting documents</p>
<div style="border:1px solid #e5e5e5;padding:16px;border-radius:2px;margin-bottom:20px">
  <p style="font-size:13px;color:#555;margin:0 0 12px">
    No legislation was voted on at this meeting. The agenda documents are linked below.
  </p>
  {rows}
</div>"""


def render_summary_email(
    body: str,
    date_str: str,
    summary: str,
    items: list[dict],
    routine_count: int,
    unsub_url: str,
    meeting_id: str = "",
    meeting_docs: list[dict] | None = None,
) -> tuple[str, str]:
    """Return (subject, html) for a post-meeting summary email."""
    fmt_date = _fmt_date(date_str)
    subject = f"{body} Meeting Summary — {fmt_date}"

    non_routine = [i for i in items if not i.get("isRoutine", False)]
    display = non_routine[:8]
    overflow = len(non_routine) - len(display)

    meeting_link = _meeting_link_html(meeting_id, "View this meeting at thegovernmentandme.tools")

    if non_routine:
        count_note = f"{len(non_routine)} non-routine item{'s' if len(non_routine) != 1 else ''}"
        if routine_count:
            count_note += f", {routine_count} routine item{'s' if routine_count != 1 else ''} (claims, permits, communications)"

        cards = "".join(_matter_card_html(i) for i in display)
        more = ""
        if overflow > 0:
            more = f'<div class="more-link"><a href="{APP_URL}?meeting={html.escape(meeting_id or "", quote=True)}#legislation">+ {overflow} more item{"s" if overflow != 1 else ""} — view at thegovernmentandme.tools</a></div>'

        matters_html = f"""
<p class="section-label">Non-routine matters ({len(non_routine)})</p>
<p class="count-note">{_h(count_note)}</p>
{cards}
{more}"""
    else:
        matters_html = _meeting_docs_html(meeting_docs or [])

    body_html = f"""
<p class="section-label">What happened</p>
<div class="overview">{_h(summary)}</div>
{matters_html}
<div style="margin-top:20px;font-size:13px">{meeting_link}</div>"""

    return subject, _shell(body, body, f"Meeting Summary — {fmt_date}", body_html, unsub_url)


def render_agenda_email(
    body: str,
    date_str: str,
    location: str,
    items: list[dict],
    routine_count: int,
    unsub_url: str,
    meeting_id: str = "",
    meeting_docs: list[dict] | None = None,
    public_comment_deadline: str = "",
) -> tuple[str, str]:
    """Return (subject, html) for a pre-meeting agenda email."""
    fmt_date = _fmt_date(date_str)
    subject = f"Agenda Published: {body} Meeting on {fmt_date}"

    non_routine = [i for i in items if not i.get("isRoutine", False)]
    display = non_routine[:8]
    overflow = len(non_routine) - len(display)

    loc_html = f'<p class="meta-row"><strong>Where:</strong> {_h(location)}</p>' if location else ""
    meeting_link = _meeting_link_html(meeting_id, "View this meeting at thegovernmentandme.tools")

    comment_html = ""
    if public_comment_deadline:
        comment_html = f"""
<div class="overview" style="margin-top:16px;margin-bottom:24px">
  <strong>Want to weigh in?</strong> Public comments are due by {_h(public_comment_deadline)}.<br><br>
  Submit your comment online at the City Clerk's ELMS portal:<br>
  <a href="https://chicityclerkelms.chicago.gov/Meetings/" style="color:#1a6aad;">
    chicityclerkelms.chicago.gov/Meetings →</a>
</div>"""

    if non_routine:
        total = len(non_routine) + routine_count
        count_note = f"{total} item{'s' if total != 1 else ''} scheduled"
        if routine_count:
            count_note += f" — {len(non_routine)} legislation item{'s' if len(non_routine) != 1 else ''}, {routine_count} routine"

        cards = "".join(_matter_card_html(i, show_status=True) for i in display)
        more = ""
        if overflow > 0:
            more = f'<div class="more-link"><a href="{APP_URL}?meeting={html.escape(meeting_id or "", quote=True)}#legislation">+ {overflow} more item{"s" if overflow != 1 else ""} — view full agenda at thegovernmentandme.tools</a></div>'

        agenda_section = f"""
<p class="count-note" style="margin-top:12px">{_h(count_note)}</p>
<p class="section-label" style="margin-top:20px">What's on the agenda</p>
{cards}
{more}"""
    else:
        agenda_section = _meeting_docs_html(meeting_docs or [])

    body_html = f"""
<p class="section-label">Meeting coming up</p>
<p class="meta-row"><strong>When:</strong> {_h(fmt_date)}</p>
{loc_html}
{comment_html}
{agenda_section}
<div style="margin-top:20px;font-size:13px">{meeting_link}</div>"""

    return subject, _shell("Upcoming Meeting", body, f"Agenda Published — {fmt_date}", body_html, unsub_url)


def render_new_meeting_email(
    body: str,
    date_str: str,
    meeting_time: str,
    location: str,
    items: list[dict],
    routine_count: int,
    public_comment_deadline: str,
    unsub_url: str,
) -> tuple[str, str]:
    """Return (subject, html) for a 'new meeting scheduled' alert.

    Sent the moment sync_meeting_schedule() discovers a meeting not previously
    in known_meetings. Includes the public comment deadline from ELMS and
    whatever agenda items are already published (may be empty for newly listed
    meetings whose agendas haven't been posted yet).
    """
    fmt_date = _fmt_date(date_str)
    subject  = f"New {body} Meeting — {fmt_date}"

    when_line = fmt_date
    if meeting_time:
        when_line += f", {meeting_time}"
    loc_html = f'<p class="meta-row"><strong>Where:</strong> {_h(location)}</p>' if location else ""

    deadline_html = ""
    if public_comment_deadline:
        deadline_html = f"""
<div class="overview" style="margin-top:16px">
  <strong>Public comment deadline:</strong> {_h(public_comment_deadline)}<br>
  <a href="https://chicityclerkelms.chicago.gov/Meetings/" style="color:#1a6aad;">
    Submit a comment on ELMS →</a>
</div>"""

    if items:
        display  = items[:8]
        overflow = len(items) - len(display)
        total    = len(items) + routine_count
        count_note = f"{total} item{'s' if total != 1 else ''} on the agenda"
        if routine_count:
            count_note += f" ({len(items)} non-routine, {routine_count} routine)"
        cards = "".join(_matter_card_html(i, show_status=False) for i in display)
        more  = (f'<div class="more-link"><a href="{APP_URL}">+ {overflow} more — view full agenda</a></div>'
                 if overflow > 0 else "")
        agenda_html = f"""
<p class="section-label" style="margin-top:24px">What's on the agenda</p>
<p class="count-note">{_h(count_note)}</p>
{cards}{more}"""
    else:
        agenda_html = (
            '<p class="count-note" style="color:#888;font-style:italic">'
            "The full agenda has not been published yet. "
            "You'll receive another email once it is.</p>"
        )

    body_html = f"""
<p class="section-label">Meeting details</p>
<p class="meta-row"><strong>When:</strong> {_h(when_line)}</p>
{loc_html}
{deadline_html}
{agenda_html}"""

    return subject, _shell(body, "New Meeting Scheduled", f"{body} — {fmt_date}", body_html, unsub_url)


def render_matter_update_email(
    record_number: str,
    plain_title: str,
    old_status: str,
    new_status: str,
    change_date: str,
    attachment_summary: str | None,
    unsub_url: str,
) -> tuple[str, str]:
    """Return (subject, html) for a matter status-change email."""
    subject = f"Update on {record_number} — {plain_title}"
    matter_url = f"{APP_URL}?matter={html.escape(record_number, quote=True)}"
    fmt_date = _fmt_date(change_date) if change_date else ""

    new_label = _h(new_status)
    if fmt_date:
        new_label += f" ({_h(fmt_date)})"

    summary_html = ""
    if attachment_summary:
        summary_html = f'<p class="matter-summary">{_h(attachment_summary)}</p>'
    else:
        summary_html = '<p class="matter-summary" style="color:#888;font-style:italic">No attachment summary available.</p>'

    body_html = f"""
<p class="section-label">Legislation update</p>
<div class="matter" style="margin-bottom:20px">
  <div class="matter-head">{_h(record_number)}</div>
  <div class="matter-title">{_h(plain_title)}</div>
  <div class="status-change" style="margin-top:12px">
    <div class="label">Status change</div>
    <div class="row"><strong>Was:</strong> {_h(old_status)}</div>
    <div class="row"><strong>Now:</strong> {new_label}</div>
  </div>
  {summary_html}
  <div class="matter-link">→ <a href="{matter_url}">View full details at thegovernmentandme.tools</a></div>
</div>"""

    return subject, _shell("Legislation Update", plain_title, record_number, body_html, unsub_url)
