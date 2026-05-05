import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timezone

import requests as _http

from ..claude_client import _claude_create
from ..config import CLAUDE_PRIMARY
from ..db import _db

logger = logging.getLogger(__name__)

ELMS_BASE = "https://api.chicityclerkelms.chicago.gov"

RELEVANCE_CUTOFF = 4

_BOILERPLATE_KEYWORDS = [
    "damage claim", "damage to vehicle", "permit fee waiver",
    "parking", "ticket adjudication", "damaged vehicle",
    "tax levy", "rebate", "honorarium",
]

_ROUTINE_MATTER_TYPES = {"claim", "communication", "report", "oath"}

_plain_language_cache: dict[str, str] = {}

_COMMITTEE_CHAIRS: dict[str, dict] = {
    "Committee on Aviation":                            {"name": "Ald. Matt O'Shea",              "ward": 19},
    "Committee on Budget and Government Operations":    {"name": "Ald. Jason Ervin",               "ward": 28},
    "Committee on Committees, Rules and Ethics":        {"name": "Ald. Michelle Harris",           "ward": 8},
    "Committee on Contracting Oversight and Equity":    {"name": "Ald. David Moore",               "ward": 17},
    "Committee on Economic, Capital and Technology Development": {"name": "Ald. Derrick Curtis",   "ward": 18},
    "Committee on Education and Child Development":     {"name": "Ald. Jeanette Taylor",           "ward": 20},
    "Committee on Environmental Protection and Energy": {"name": "Ald. Maria Hadden",              "ward": 49},
    "Committee on Ethics and Government Oversight":     {"name": "Ald. Matt Martin",               "ward": 47},
    "Committee on Finance":                             {"name": "Ald. Pat Dowell",                "ward": 3},
    "Committee on Health and Human Relations":          {"name": "Ald. Rossana Rodríguez-Sánchez", "ward": 33},
    "Committee on Housing and Real Estate":             {"name": "Ald. Byron Sigcho-Lopez",        "ward": 25},
    "Committee on Immigrant and Refugee Rights":        {"name": "Ald. Jessie Fuentes",            "ward": 26},
    "Committee on License and Consumer Protection":     {"name": "Ald. Debra Silverstein",         "ward": 50},
    "Committee on Pedestrian and Traffic Safety":       {"name": "Ald. Andre Vasquez",             "ward": 40},
    "Committee on Police and Fire":                     {"name": "Ald. Chris Taliaferro",          "ward": 29},
    "Committee on Public Safety":                       {"name": "Ald. Brian Hopkins",             "ward": 2},
    "Committee on Special Events, Cultural Affairs and Recreation": {"name": "Ald. Nick Sposato",  "ward": 38},
    "Committee on Transportation and Public Way":       {"name": "Ald. Greg Mitchell",             "ward": 7},
    "Committee on Workforce Development":               {"name": "Ald. Michael Rodriguez",         "ward": 22},
    "Committee on Zoning, Landmarks and Building Standards": {"name": "Ald. Daniel La Spata",     "ward": 1},
}

_LEGISLATION_TYPES: dict[str, str] = {
    "ordinance":   "An ordinance creates, amends, or repeals a law in the Chicago Municipal Code. It has full legal force once signed by the Mayor (or after a veto override).",
    "resolution":  "A resolution expresses the City Council's opinion, honors an individual or organization, or calls for a specific action. It does not have the force of law.",
    "order":       "An order directs a city department or official to take a specific administrative action — such as issuing a permit, making a public-way repair, or conducting a study. It does not amend the Municipal Code.",
    "appointment": "An appointment confirms a person nominated by the Mayor to serve on a city board, commission, or authority (e.g., the Chicago Transit Authority or Plan Commission).",
    "claim":       "A claim is a compensation request from a resident or business for damages caused by city property, vehicles, or employees. Most are routine and processed in bulk.",
    "oath":        "An oath records the swearing-in of a newly elected or appointed official.",
    "communication": "A communication is information submitted to the Council by the Mayor or a city department for the record. No vote is required.",
    "report":      "A formal report submitted to the City Council, typically from a city department, inspector general, or oversight body.",
}

_STATUS_CONTEXT: dict[str, str] = {
    "in_committee_active": (
        "This legislation is currently under review by the committee listed above. "
        "Pursuant to City Council Rule 41, all legislation is automatically referred to committee after introduction. "
        "The committee will schedule a public hearing before voting to advance or reject it."
    ),
    "in_committee_stale": (
        "This legislation has been in committee for more than 180 days with no recorded action. "
        "Under City Council rules, any alderperson can move to discharge it from committee with a majority vote, "
        "but this rarely happens. The legislation may effectively be dead."
    ),
    "held_in_committee": (
        "The legislation failed to receive enough committee votes to advance. "
        "This effectively stalls or kills the measure — no further action is expected this session. "
        "A committee can hold a bill when members want more time, when it lacks support, "
        "or when leadership wants to avoid a full council vote."
    ),
    "referred": (
        "Pursuant to City Council Rule 41, all proposed legislation is automatically referred to a committee "
        "for consideration and only acted upon by the City Council at a subsequent meeting."
    ),
    "passed": (
        "The City Council voted to approve this legislation. "
        "If it is an ordinance, it has the force of law once signed by the Mayor. "
        "If it is a resolution, it expresses the Council's official position."
    ),
    "failed": (
        "The City Council voted against this legislation. It did not pass and requires reintroduction to be reconsidered."
    ),
    "withdrawn": (
        "The sponsor(s) withdrew this legislation before a final vote. "
        "It can be reintroduced in a future session."
    ),
    "tabled": (
        "The City Council voted to table (postpone indefinitely) consideration of this legislation. "
        "It can be brought back off the table at a future meeting with a majority vote."
    ),
}


def preload_plain_language_cache() -> None:
    """Load all cached plain-language titles from DB into memory at startup."""
    global _plain_language_cache
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT record_number, plain_title FROM plain_language_titles")
            rows = cur.fetchall()
            for rn, pt in rows:
                _plain_language_cache[rn] = pt
        print(f"Loaded {len(_plain_language_cache)} cached plain language titles from DB", flush=True)
    except Exception as exc:
        print(f"Warning: could not preload plain language titles: {exc}", flush=True)


def _elms_get(path, params=None):
    resp = _http.get(ELMS_BASE + path, params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _is_boilerplate(matter) -> bool:
    title = (matter.get("title") or "").lower()
    return any(kw in title for kw in _BOILERPLATE_KEYWORDS)


def _classify_routine(matter_type: str, matter_title: str) -> bool:
    if (matter_type or "").lower() in _ROUTINE_MATTER_TYPES:
        return True
    return _is_boilerplate({"title": matter_title})


def _attachment_summary(url: str, file_name: str = "") -> str | None:
    """Download a PDF attachment, extract text, summarize at 5th-grade level. DB-cached by URL hash."""
    from io import BytesIO
    url_hash = hashlib.md5(url.encode()).hexdigest()

    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT summary FROM attachment_summaries WHERE url_hash = %s", (url_hash,))
            row = cur.fetchone()
            if row:
                return row[0]
    except Exception:
        pass

    try:
        resp = _http.get(url, timeout=20, stream=True)
        resp.raise_for_status()
        content = b""
        for chunk in resp.iter_content(65536):
            content += chunk
            if len(content) > 5 * 1024 * 1024:
                break
    except Exception:
        return None

    content_type = resp.headers.get("content-type", "").lower()
    is_pdf = "pdf" in content_type or content[:5] == b"%PDF-"
    if not is_pdf:
        return None

    _SUMMARY_PROMPT = (
        "Summarize this Chicago city government document in 2-3 plain sentences at a 5th grade reading level. "
        "Write only about what the legislation proposes or would do — use present tense (e.g. 'proposes', "
        "'would require', 'calls for'). Never say it passed, was approved, or took effect. "
        "Be specific about what it involves, who it affects, and where (if mentioned). "
        "Do not use markdown, headers, bullet points, or any formatting — plain prose only."
    )

    text = ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(content))
        for page in reader.pages[:10]:
            text += page.extract_text() or ""
            if len(text) > 4000:
                break
        text = text[:4000].strip()
    except Exception:
        pass

    try:
        if len(text) >= 50:
            ai_resp = _claude_create(
                model=CLAUDE_PRIMARY,
                max_tokens=200,
                messages=[{"role": "user", "content": _SUMMARY_PROMPT + "\n\n" + text}],
            )
        else:
            ai_resp = _claude_create(
                model=CLAUDE_PRIMARY,
                max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": base64.b64encode(content).decode(),
                            },
                        },
                        {"type": "text", "text": _SUMMARY_PROMPT},
                    ],
                }],
            )
        summary = ai_resp.content[0].text.strip()
    except Exception:
        return None

    if summary:
        try:
            with _db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO attachment_summaries (url_hash, url, file_name, summary) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (url_hash, url, file_name or None, summary)
                )
        except Exception:
            pass

    return summary


def plain_language_titles(matters: list) -> dict:
    """Translate formal matter titles to plain language via Claude. Memory + DB cached."""
    uncached = [m for m in matters if m.get("recordNumber") not in _plain_language_cache]

    if uncached:
        try:
            rns = [m["recordNumber"] for m in uncached if m.get("recordNumber")]
            with _db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT record_number, plain_title FROM plain_language_titles WHERE record_number = ANY(%s)",
                    (rns,)
                )
                for rn, pt in cur.fetchall():
                    _plain_language_cache[rn] = pt
        except Exception:
            pass

    still_uncached = [m for m in matters if m.get("recordNumber") not in _plain_language_cache]
    if still_uncached:
        items = "\n".join(
            f'{{"id": "{m.get("recordNumber")}", "title": "{(m.get("title") or "").replace(chr(34), "")[:150]}"}}'
            for m in still_uncached
        )
        prompt = (
            "Translate these Chicago City Council matter titles into plain English. "
            "Each translation should be 1 sentence, ≤15 words, no legal jargon, written for a general audience. "
            'Return ONLY a JSON object mapping each id to its plain English translation. Example: {"O2025-001": "Adds protected bike lanes on Milwaukee Ave"}\n\n'
            f"[{items}]"
        )
        try:
            resp = _claude_create(
                model=CLAUDE_PRIMARY,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                translations = json.loads(match.group())
                for rn, plain in translations.items():
                    _plain_language_cache[rn] = plain
                if translations:
                    orig_titles = {m.get("recordNumber"): (m.get("title") or "")[:500] for m in still_uncached}
                    try:
                        with _db() as conn:
                            cur = conn.cursor()
                            for rn, plain in translations.items():
                                cur.execute(
                                    "INSERT INTO plain_language_titles (record_number, original_title, plain_title) "
                                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                    (rn, orig_titles.get(rn), plain)
                                )
                    except Exception:
                        pass
        except Exception:
            pass

    return {
        m.get("recordNumber"): _plain_language_cache.get(m.get("recordNumber"))
        for m in matters
    }


def claude_rerank(query: str, matters: list) -> list:
    if not matters:
        return matters
    numbered = "\n".join(
        f"{i+1}. [{m.get('recordNumber', '')}] {(m.get('title') or '')[:120]}"
        for i, m in enumerate(matters)
    )
    prompt = (
        f"User query: {query}\n\n"
        "Score each Chicago City Council matter by relevance to the query (1=irrelevant, 10=exact match). "
        "Return ONLY a JSON array of objects ordered from most to least relevant, omitting nothing. "
        'Format: [{"id": "O2025-...", "score": 8}, ...]. No other text.\n\n'
        f"{numbered}"
    )
    try:
        resp = _claude_create(
            model=CLAUDE_PRIMARY,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if not match:
            return matters
        scored = json.loads(match.group())
        id_to_matter = {m.get("recordNumber"): m for m in matters}
        reranked = []
        seen = set()
        for item in scored:
            rid = item.get("id")
            score = item.get("score", 0)
            if rid in id_to_matter and score >= RELEVANCE_CUTOFF:
                reranked.append(id_to_matter[rid])
                seen.add(rid)
        reranked += [m for m in matters if m.get("recordNumber") not in seen and m.get("recordNumber") not in {item.get("id") for item in scored}]
        return reranked
    except Exception:
        return matters


_CACHE_TTL_ACTIVE  = 4 * 3600    # 4 hours
_CACHE_TTL_SETTLED = 7 * 86400   # 7 days
_SETTLED_STATUSES  = {"passed", "failed", "withdrawn", "tabled", "vetoed"}


def get_enriched_matter(record_number: str) -> dict:
    """Return enriched matter data, reading from matter_detail_cache before hitting ELMS."""
    from datetime import timedelta
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT cached_at, status, data FROM matter_detail_cache WHERE record_number = %s",
                (record_number,),
            )
            row = cur.fetchone()
            if row:
                cached_at, cached_status, data = row
                status_lower = (cached_status or "").lower()
                settled = any(s in status_lower for s in _SETTLED_STATUSES)
                ttl = _CACHE_TTL_SETTLED if settled else _CACHE_TTL_ACTIVE
                age = (datetime.now(timezone.utc) - cached_at.replace(tzinfo=timezone.utc)).total_seconds()
                if age < ttl:
                    return data if isinstance(data, dict) else json.loads(data)
    except Exception as e:
        logger.warning("[elms] matter_detail_cache read error: %s", e)

    matter = _elms_get(f"/matter/recordNumber/{record_number}")
    matter = enrich_matter(matter)
    pt = plain_language_titles([matter])
    matter["plainLanguageTitle"] = pt.get(matter.get("recordNumber"))

    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO matter_detail_cache (record_number, status, data)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (record_number) DO UPDATE
                     SET cached_at = NOW(), status = EXCLUDED.status, data = EXCLUDED.data""",
                (record_number, matter.get("status"), json.dumps(matter)),
            )
    except Exception as e:
        logger.warning("[elms] matter_detail_cache write error: %s", e)

    return matter


def enrich_matter(matter: dict) -> dict:
    actions = matter.get("actions") or []
    meeting_id_map = {}
    for action in actions:
        body = action.get("actionByName", "")
        date_str = (action.get("actionDate") or "")[:10]
        if not body or not date_str or (body, date_str) in meeting_id_map:
            continue
        try:
            raw = _elms_get("/meeting-agenda", {"search": body, "top": 200})
            meetings = raw.get("value", raw.get("data", []))
            for meeting in meetings:
                if (meeting.get("date") or "")[:10] == date_str:
                    meeting_id = meeting.get("meetingId") or meeting.get("id")
                    if meeting_id:
                        meeting_id_map[(body, date_str)] = meeting_id
                    break
        except Exception:
            pass

    meeting_details = {}
    for key, meeting_id in meeting_id_map.items():
        try:
            detail = _elms_get(f"/meeting-agenda/{meeting_id}")
            video = detail.get("videoLink")
            detail["videoLinks"] = [video] if isinstance(video, str) and video else (video if isinstance(video, list) else [])
            meeting_details[key] = detail
        except Exception:
            pass

    for action in actions:
        body = action.get("actionByName", "")
        date_str = (action.get("actionDate") or "")[:10]
        detail = meeting_details.get((body, date_str))
        if detail:
            action["meetingDetails"] = detail

    for action in actions:
        if "refer" in (action.get("actionName") or "").lower():
            action["statusContext"] = _STATUS_CONTEXT["referred"]

    matter_type_raw = (matter.get("type") or "")
    direct_attachments = [f for f in (matter.get("attachments") or []) if f.get("path") or f.get("url")]
    for att in direct_attachments:
        url = att.get("path") or att.get("url") or ""
        if url:
            summary = _attachment_summary(url, file_name=att.get("fileName") or att.get("name") or "")
            if summary:
                att["summary"] = summary
    if direct_attachments:
        matter["matterAttachments"] = direct_attachments

    matter_type = matter_type_raw.lower()
    for key, desc in _LEGISLATION_TYPES.items():
        if key in matter_type:
            matter["typeDescription"] = desc
            break

    raw_status = (matter.get("status") or "").lower()
    raw_sub    = (matter.get("subStatus") or matter.get("substatus") or "").lower()
    display    = re.sub(r'^\d+-', '', raw_status).strip()
    intro_date = matter.get("introductionDate")
    days_old   = 0
    if intro_date:
        try:
            dt = datetime.fromisoformat(intro_date.replace("Z", "+00:00"))
            days_old = (datetime.now(timezone.utc) - dt).days
        except Exception:
            pass

    if "held in committee" in display:
        matter["statusContext"] = _STATUS_CONTEXT["held_in_committee"]
    elif "in committee" in display:
        matter["statusContext"] = _STATUS_CONTEXT["in_committee_stale" if days_old > 180 else "in_committee_active"]
    elif "refer" in display:
        matter["statusContext"] = _STATUS_CONTEXT["referred"]
    elif re.search(r'pass|adopt|approv', re.sub(r'^\d+-', '', raw_sub or display)):
        matter["statusContext"] = _STATUS_CONTEXT["passed"]
    elif re.search(r'fail|reject|defeat', re.sub(r'^\d+-', '', raw_sub or display)):
        matter["statusContext"] = _STATUS_CONTEXT["failed"]
    elif "withdraw" in display:
        matter["statusContext"] = _STATUS_CONTEXT["withdrawn"]
    elif "table" in display:
        matter["statusContext"] = _STATUS_CONTEXT["tabled"]

    controlling_body = matter.get("controllingBody") or ""
    chair = None
    for committee_name, chair_info in _COMMITTEE_CHAIRS.items():
        if committee_name.lower() in controlling_body.lower() or controlling_body.lower() in committee_name.lower():
            chair = chair_info
            break
    if chair:
        matter["committeeChair"] = chair

    what_can_you_do = []
    if "in committee" in display or "refer" in display:
        what_can_you_do.append({
            "action": "Contact your alderperson",
            "detail": "Your ward's alderperson can advocate for or against this legislation in committee and on the Council floor.",
            "link": "https://www.chicago.gov/city/en/depts/mayor/provdrs/your_ward_and_alderman/svcs/find_my_alderman.html",
            "linkText": "Find your alderperson",
        })
        if chair:
            what_can_you_do.append({
                "action": f"Contact the committee chair: {chair['name']}",
                "detail": f"As chair of the {controlling_body}, this alderperson controls when and whether the legislation receives a hearing.",
                "link": f"https://www.chicago.gov/city/en/about/wards/{chair['ward']:02d}thward.html",
                "linkText": f"Ward {chair['ward']} office",
            })
        what_can_you_do.append({
            "action": "Watch the committee meeting",
            "detail": "Committee meetings are open to the public. Live streams and archives are on the City Clerk's website.",
            "link": "https://www.chicityclerk.com/city-council-news-and-events/city-council-and-committee-meetings",
            "linkText": "View upcoming meetings",
        })
    elif "pass" in display or (raw_sub and re.search(r'pass|adopt', raw_sub)):
        what_can_you_do.append({
            "action": "Read the full text",
            "detail": "The ordinance or resolution text is available in the attachments above or on the City Clerk's website.",
            "link": "https://chicityclerkelms.chicago.gov",
            "linkText": "Chicago City Clerk eLMS",
        })

    # Always add: contact the filing sponsor (prepend so it's first)
    filing_sponsor_raw = matter.get("filingSponsor") or ""
    if filing_sponsor_raw:
        fs_clean = filing_sponsor_raw.lower().replace("ald.", "").replace(",", "").replace(".", "").strip()
        sponsor_ward_info = None
        for _, chair_info in _COMMITTEE_CHAIRS.items():
            chair_clean = chair_info["name"].lower().replace("ald.", "").replace(".", "").strip()
            # Match on last name (last word of the chair name)
            chair_last = chair_clean.split()[-1] if chair_clean.split() else ""
            if chair_last and len(chair_last) > 3 and chair_last in fs_clean:
                sponsor_ward_info = chair_info
                break
        if sponsor_ward_info:
            what_can_you_do.insert(0, {
                "action": f"Contact the sponsor: {sponsor_ward_info['name']}",
                "detail": "The filing sponsor introduced this legislation and can answer questions about it.",
                "link": f"https://www.chicago.gov/city/en/about/wards/{sponsor_ward_info['ward']:02d}thward.html",
                "linkText": f"Ward {sponsor_ward_info['ward']} alderperson's office",
            })
        else:
            what_can_you_do.insert(0, {
                "action": f"Contact the sponsor: {filing_sponsor_raw}",
                "detail": "The filing sponsor introduced this legislation and can answer questions about it.",
                "link": "https://www.chicago.gov/city/en/depts/mayor/provdrs/your_ward_and_alderman/svcs/find_my_alderman.html",
                "linkText": "Find alderperson contact info",
            })

    if what_can_you_do:
        matter["whatCanYouDo"] = what_can_you_do

    return matter


def meeting_summary(meeting_id: str, body: str, date_str: str, items: list) -> str:
    """Generate a ≤50-word summary of a meeting. DB-cached by meeting_id."""
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT summary FROM meeting_summaries WHERE meeting_id = %s", (meeting_id,))
            row = cur.fetchone()
            if row:
                return row[0]
    except Exception:
        pass

    non_routine = [i for i in items if not i.get("isRoutine")]
    routine_count = sum(1 for i in items if i.get("isRoutine"))

    if not non_routine and not items:
        return ""

    nr_titles = "\n".join(
        f"- {i.get('matterType', 'Item')}: {(i.get('matterTitle') or '')[:120]}"
        for i in non_routine[:20]
    )
    prompt = (
        f"Summarize this Chicago City Council {body} meeting ({date_str}) in 50 words or fewer. "
        "Focus on the non-routine items with specific detail (what action, what place or dollar amount). "
        f"Mention routine items only as a count ({routine_count} routine items). "
        "Write at a 5th grade reading level. Do not use bullet points — write 2-3 plain sentences.\n\n"
        f"Non-routine items:\n{nr_titles if nr_titles else '(none)'}\n"
        f"Routine items: {routine_count}"
    )
    try:
        resp = _claude_create(
            model=CLAUDE_PRIMARY,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = resp.content[0].text.strip()
    except Exception:
        summary = f"{len(non_routine)} non-routine and {routine_count} routine items considered."

    if summary:
        try:
            with _db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO meeting_summaries (meeting_id, body, meeting_date, summary) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (meeting_id, body, date_str, summary)
                )
        except Exception:
            pass

    return summary


def fetch_meeting_items(meeting_id: str) -> list:
    """Fetch and classify agenda items for a meeting."""
    detail = _elms_get(f"/meeting-agenda/{meeting_id}")
    groups = (detail.get("agenda") or {}).get("groups", [])
    items = []
    for group in groups:
        items.extend(group.get("items") or [])
    for item in items:
        item["isRoutine"] = _classify_routine(
            item.get("matterType", ""), item.get("matterTitle", "")
        )
    return items


def fetch_matter_detail_slim(record_number: str) -> dict:
    """Fetch just status/introductionDate/controllingBody for a matter."""
    try:
        m = _elms_get(f"/matter/recordNumber/{record_number}")
        return {
            "recordNumber": record_number,
            "status": m.get("status"),
            "substatus": m.get("subStatus"),
            "introductionDate": m.get("introductionDate"),
            "controllingBody": m.get("controllingBody"),
        }
    except Exception:
        return {"recordNumber": record_number}
