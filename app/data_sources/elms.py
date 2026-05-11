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
    "recommended_to_pass": (
        "The committee has recommended that the full City Council approve this legislation. "
        "It has NOT yet been voted on by the full City Council — that vote is the next step."
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


_next_meeting_cache: dict[str, tuple[float, dict | None]] = {}
_NEXT_MEETING_CACHE_TTL = 30 * 60  # 30 minutes


def _next_meeting_for_body(body: str) -> dict | None:
    """Return the next upcoming meeting for a controlling body, with publicCommentDeadline.

    Result is cached per body for 30 minutes to avoid redundant ELMS calls on
    every matter detail view.
    """
    import time as _time
    cached = _next_meeting_cache.get(body)
    if cached:
        ts, result = cached
        if _time.time() - ts < _NEXT_MEETING_CACHE_TTL:
            return result

    today = datetime.now(timezone.utc).date().isoformat()
    result = None
    try:
        raw = _elms_get("/meeting-agenda", {"search": body, "top": 50, "orderby": "date asc"})
        meetings = raw.get("value", raw.get("data", []))
        for m in meetings:
            date_str = (m.get("date") or "")[:10]
            if date_str < today:
                continue
            meeting_id = m.get("meetingId") or m.get("id")
            if not meeting_id:
                continue
            try:
                full = _elms_get(f"/meeting-agenda/{meeting_id}")
                result = {
                    "meetingId": meeting_id,
                    "date": m.get("date") or date_str,
                    "publicCommentDeadline": full.get("publicCommentDeadline"),
                }
            except Exception:
                result = {"meetingId": meeting_id, "date": m.get("date") or date_str, "publicCommentDeadline": None}
            break
    except Exception:
        pass

    _next_meeting_cache[body] = (_time.time(), result)
    return result


def base_number(record_number: str) -> str:
    """Strip the leading alpha prefix from a record number, returning the numeric base.

    O2026-0024166, F2026-0024166, SO2026-0024166 all return '2026-0024166'.
    This lets callers treat different-prefix variants of the same matter as identical.
    """
    i = 0
    while i < len(record_number) and record_number[i].isalpha():
        i += 1
    return record_number[i:]


def _s_variant(record_number: str) -> str:
    """Return the substituted-matter variant of a record number.

    Chicago eLMS adds an 'S' before the type letter when a matter is substituted,
    e.g. O2026-0023357 → SO2026-0023357.  If the number already starts with 'S'
    (i.e. it is itself the substituted form), return it unchanged so callers can
    always pass either form.
    """
    if record_number.startswith("S") and len(record_number) > 1 and not record_number[1].isdigit():
        return record_number  # already the substituted form
    return "S" + record_number


def _merge_variant_actions(variants: list[dict]) -> list[dict]:
    """Return a deduplicated, date-sorted union of actions from all matter variants."""
    seen: set[str] = set()
    merged: list[dict] = []
    for v in variants:
        for action in (v.get("actions") or []):
            key = str(action.get("id") or
                      f"{action.get('actionDate')}|{action.get('actionName')}|{action.get('actionByName')}")
            if key not in seen:
                seen.add(key)
                merged.append(action)
    merged.sort(key=lambda a: (a.get("actionDate") or ""))
    return merged


def get_enriched_matter(record_number: str) -> dict:
    """Return enriched matter data, reading from matter_detail_cache before hitting ELMS.

    User-facing reads always serve whatever is in the cache — no TTL check.
    The scheduler owns freshness: it re-warms matter_detail_cache whenever it
    processes a meeting, so the cache is only stale between scheduler runs (hours),
    not between user loads.  A cache miss only fires for matters the scheduler
    has never seen.

    All prefix variants sharing the same base number (e.g., O2026-0024166 and
    F2026-0024166) are fetched from cache and their action histories merged, so
    the returned matter shows the full legislative history regardless of which
    prefix the caller used.
    """
    base = base_number(record_number)
    s_number = _s_variant(record_number)

    variants: list[dict] = []
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT data FROM matter_detail_cache
                   WHERE record_number LIKE %s
                   ORDER BY (status IS NOT NULL) DESC, cached_at DESC""",
                (f"%{base}",),
            )
            for row in cur.fetchall():
                data = row[0]
                variants.append(data if isinstance(data, dict) else json.loads(data))
    except Exception as e:
        logger.warning("[elms] matter_detail_cache read error: %s", e)

    if variants:
        primary = dict(variants[0])
        if len(variants) > 1:
            merged = _merge_variant_actions(variants)
            if merged != primary.get("actions"):
                primary["actions"] = merged
                primary["legislativeTracker"] = _build_legislative_tracker(primary)
        return primary

    try:
        matter = _elms_get(f"/matter/recordNumber/{record_number}")
    except Exception:
        # Substituted matters get an S-prefixed record number; try that fallback.
        matter = _elms_get(f"/matter/recordNumber/{s_number}")
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


def _build_legislative_tracker(matter: dict) -> list:
    actions = matter.get("actions") or []  # already sorted ascending
    matter_type = (matter.get("type") or "").lower()

    _MAYOR_NA_TYPES = {"resolution", "order", "appointment", "claim",
                       "communication", "oath", "report"}
    mayor_na = any(t in matter_type for t in _MAYOR_NA_TYPES)

    referred_action = committee_hearing_action = committee_outcome_action = None
    council_vote_action = mayor_action_action = None
    blocked_at_committee = blocked_at_council = blocked_at_mayor = False
    withdrawn_overall = False

    for action in actions:
        name = (action.get("actionName") or "").lower().strip()
        committee_body = (action.get("actionByName") or "").lower()

        if referred_action is None and "refer" in name:
            referred_action = action

        def _is_committee_outcome(n):
            return (
                n.startswith("recommend")
                or n in {"tabled", "held in committee", "substituted",
                         "deferred and published", "failed to pass",
                         "passed", "passed as amended", "approved", "approved as amended"}
                or any(kw in n for kw in ["substitute recommended", "approved as substituted"])
            )

        if committee_hearing_action is None and "refer" not in name:
            if "committee" in committee_body or _is_committee_outcome(name):
                committee_hearing_action = action

        if _is_committee_outcome(name):
            committee_outcome_action = action
            blocked_at_committee = name in ("tabled", "held in committee")

        def _is_council_vote(n):
            return (
                n.startswith("passed") or n.startswith("approved")
                or n in {"adopted", "failed to pass"}
                or n.startswith("adopted")
            )

        if council_vote_action is None and _is_council_vote(name) and "council" in committee_body:
            council_vote_action = action
            if "fail" in name:
                blocked_at_council = True

        if mayor_action_action is None and ("sign" in name or "veto" in name):
            mayor_action_action = action
            if "veto" in name:
                blocked_at_mayor = True

        if "withdraw" in name:
            withdrawn_overall = True

    def _step(sid, label, sublabel, status, action_obj):
        return {
            "id": sid, "label": label, "sublabel": sublabel, "status": status,
            "date": (action_obj.get("actionDate") or None) if action_obj else None,
            "actionName": (action_obj.get("actionName") or None) if action_obj else None,
            "actionByName": (action_obj.get("actionByName") or None) if action_obj else None,
        }

    s1 = "complete" if referred_action else "pending"
    s2 = ("complete" if committee_hearing_action
          else "current" if s1 == "complete" and not withdrawn_overall
          else "pending")
    s3 = ("blocked" if (committee_outcome_action and blocked_at_committee)
          else "complete" if committee_outcome_action
          else "blocked" if withdrawn_overall
          else "current" if s2 == "complete"
          else "pending")
    s4 = ("blocked" if (council_vote_action and blocked_at_council)
          else "complete" if council_vote_action
          else "blocked" if (withdrawn_overall and not council_vote_action)
          else "current" if s3 == "complete"
          else "pending")
    if mayor_na:
        s5 = "not_applicable"
    else:
        s5 = ("blocked" if (mayor_action_action and blocked_at_mayor)
              else "complete" if mayor_action_action
              else "blocked" if (withdrawn_overall and not mayor_action_action)
              else "current" if s4 == "complete"
              else "pending")

    return [
        _step("referred",          "Referred to Committee", "Introduced & assigned",        s1, referred_action),
        _step("committee_hearing", "Committee Hearing",     "Discussed in committee",        s2, committee_hearing_action),
        _step("committee_outcome", "Committee Outcome",     "Recommendation or vote result", s3, committee_outcome_action),
        _step("council_vote",      "City Council Vote",     "Full council approval",         s4, council_vote_action),
        _step("mayor_action",      "Mayor's Signature",     "Signed, vetoed, or N/A",        s5, mayor_action_action),
    ]


def enrich_matter(matter: dict) -> dict:
    actions = matter.get("actions") or []
    actions.sort(key=lambda a: (a.get("actionDate") or ""))
    matter["actions"] = actions

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
    elif re.search(r'recommend', raw_sub) and re.search(r'pass|approv', raw_sub):
        matter["statusContext"] = _STATUS_CONTEXT["recommended_to_pass"]
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
        if controlling_body:
            next_mtg = _next_meeting_for_body(controlling_body)
            if next_mtg:
                deadline_iso = next_mtg.get("publicCommentDeadline") or ""
                meeting_date_iso = (next_mtg.get("date") or "")[:10]
                deadline_str = None
                if deadline_iso:
                    try:
                        from zoneinfo import ZoneInfo
                        dt_utc = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
                        dt_ct  = dt_utc.astimezone(ZoneInfo("America/Chicago"))
                        deadline_str = dt_ct.strftime("%-m/%-d/%Y at %-I:%M %p CT")
                    except Exception:
                        deadline_str = deadline_iso[:16].replace("T", " ") + " UTC"
                elif meeting_date_iso:
                    deadline_str = f"before the meeting on {meeting_date_iso}"
                if deadline_str:
                    what_can_you_do.append({
                        "action": "Submit a public comment",
                        "detail": f"Written public comments are accepted before the next committee meeting. Deadline: {deadline_str}.",
                        "link": "https://chicityclerkelms.chicago.gov/Meetings/",
                        "linkText": "Submit comment on ELMS",
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

    matter["legislativeTracker"] = _build_legislative_tracker(matter)
    return matter


_STANDARD_NO_MATTER_SUMMARY = (
    "No votes were taken at this meeting. Instead, a subject matter hearing took place. "
    "Click into this meeting to see the meeting agenda."
)

_MATTER_ID_RE = re.compile(r'\b([A-Z]{1,3}(?:-?\d{4})-\d+)\b')


def _items_from_meeting_items_table(meeting_id: str) -> list[dict]:
    """Read agenda items for a meeting directly from the meeting_items DB table."""
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT record_number, matter_id, matter_title, matter_type, action_name, is_routine
                   FROM meeting_items WHERE meeting_id = %s
                   ORDER BY is_routine ASC, item_order ASC""",
                (meeting_id,),
            )
            return [
                {
                    "recordNumber": r[0],
                    "matterId":     r[1],
                    "matterTitle":  r[2],
                    "matterType":   r[3],
                    "actionName":   r[4],
                    "isRoutine":    r[5],
                }
                for r in cur.fetchall()
            ]
    except Exception as e:
        logger.warning("[elms] _items_from_meeting_items_table %s: %s", meeting_id, e)
        return []


def _fetch_agenda_text(meeting_id: str) -> str | None:
    """Download the Agenda PDF from meeting files and return extracted text."""
    try:
        detail = _elms_get(f"/meeting-agenda/{meeting_id}")
        files = detail.get("files") or []
    except Exception:
        return None

    agenda_url = None
    for f in files:
        name = (f.get("fileName") or f.get("name") or "").lower()
        if "agenda" in name:
            agenda_url = f.get("path") or f.get("url")
            break
    if not agenda_url:
        for f in files:
            url = f.get("path") or f.get("url") or ""
            if ".pdf" in url.lower():
                agenda_url = url
                break

    if not agenda_url:
        return None

    try:
        from io import BytesIO
        from pypdf import PdfReader
        resp = _http.get(agenda_url, timeout=20, stream=True)
        resp.raise_for_status()
        content = b""
        for chunk in resp.iter_content(65536):
            content += chunk
            if len(content) > 5 * 1024 * 1024:
                break
        text = ""
        for page in PdfReader(BytesIO(content)).pages[:20]:
            text += page.extract_text() or ""
            if len(text) > 8000:
                break
        return text.strip() or None
    except Exception:
        return None


def _extract_matter_ids_from_text(text: str) -> list[str]:
    """Extract Chicago City Council record numbers from agenda text (e.g. R2026-0024891)."""
    seen: set[str] = set()
    result: list[str] = []
    for mid in _MATTER_ID_RE.findall(text):
        if mid not in seen:
            seen.add(mid)
            result.append(mid)
    return result


def _summary_from_agenda(body: str, date_str: str, agenda_text: str) -> str:
    """Generate a ≤50-word meeting summary from agenda text via Claude."""
    prompt = (
        f"Summarize this Chicago City Council {body} meeting agenda ({date_str}) in 50 words or fewer. "
        "Focus on the main topics discussed. "
        "Note: this was a subject matter hearing — no votes were taken. "
        "Write at a 5th grade reading level. Do not use bullet points — write 2-3 plain sentences.\n\n"
        f"Agenda:\n{agenda_text[:4000]}"
    )
    try:
        resp = _claude_create(
            model=CLAUDE_PRIMARY,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception:
        return _STANDARD_NO_MATTER_SUMMARY


def link_agenda_matters(meeting_id: str, body: str, date_str: str, matter_ids: list[str]) -> None:
    """Link matter IDs found in an agenda to a meeting that eLMS didn't connect them to.

    For each matter ID:
    - Skips if already in meeting_items for this meeting
    - Fetches or loads matter data from cache/ELMS
    - Injects a synthetic "Discussed in Committee" action so the legislative
      tracker shows step 2 complete / step 3 pending
    - Upserts into meeting_items with action_name "discussed in committee"
    """
    if not matter_ids:
        return

    for matter_id in matter_ids:
        try:
            with _db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT 1 FROM meeting_items WHERE meeting_id = %s AND record_number = %s",
                    (meeting_id, matter_id),
                )
                if cur.fetchone():
                    continue
        except Exception:
            pass

        matter_data: dict | None = None
        try:
            with _db() as conn:
                cur = conn.cursor()
                s_matter_id = _s_variant(matter_id)
                id_candidates = [matter_id] if s_matter_id == matter_id else [matter_id, s_matter_id]
                cur.execute(
                    "SELECT data FROM matter_detail_cache WHERE record_number = ANY(%s)",
                    (id_candidates,),
                )
                row = cur.fetchone()
                if row:
                    matter_data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        except Exception:
            pass

        if matter_data is None:
            try:
                matter_data = get_enriched_matter(matter_id)
            except Exception:
                logger.warning("[elms] link_agenda_matters: could not fetch %s", matter_id)
                matter_data = {"recordNumber": matter_id}

        matter_title = matter_data.get("title") or matter_data.get("shortTitle") or matter_id
        matter_type  = matter_data.get("type") or ""

        # Inject synthetic committee-hearing action only if one doesn't already exist
        actions = list(matter_data.get("actions") or [])
        body_lower = body.lower()
        has_hearing = any(
            "refer" not in (a.get("actionName") or "").lower()
            and (
                body_lower in (a.get("actionByName") or "").lower()
                or (a.get("actionByName") or "").lower() in body_lower
            )
            and (a.get("actionDate") or "")[:10] == date_str
            for a in actions
        )

        if not has_hearing:
            synthetic = {
                "actionName": "Discussed in Committee",
                "actionDate": f"{date_str}T00:00:00",
                "actionByName": body,
                "agendaDerived": True,
            }
            actions = sorted(actions + [synthetic], key=lambda a: (a.get("actionDate") or ""))
            matter_data["actions"] = actions
            matter_data["legislativeTracker"] = _build_legislative_tracker(matter_data)
            try:
                with _db() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        """INSERT INTO matter_detail_cache (record_number, status, data)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (record_number) DO UPDATE
                             SET cached_at = NOW(), status = EXCLUDED.status, data = EXCLUDED.data""",
                        (matter_id, matter_data.get("status"), json.dumps(matter_data)),
                    )
            except Exception as e:
                logger.warning("[elms] link_agenda_matters: cache update failed %s: %s", matter_id, e)

        try:
            with _db() as conn:
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO meeting_items
                           (meeting_id, record_number, matter_id, matter_title,
                            matter_type, action_name, is_routine, item_order)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (meeting_id, record_number) DO UPDATE
                         SET action_name = EXCLUDED.action_name,
                             cached_at   = NOW()""",
                    (meeting_id, matter_id, matter_data.get("matterId"),
                     matter_title, matter_type, "discussed in committee",
                     _classify_routine(matter_type, matter_title), 999),
                )
        except Exception as e:
            logger.warning("[elms] link_agenda_matters: meeting_items insert failed %s: %s", matter_id, e)


def meeting_summary(meeting_id: str, body: str, date_str: str, items: list) -> str:
    """Generate a ≤50-word summary of a meeting. DB-cached by meeting_id."""
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT summary FROM meeting_summaries WHERE meeting_id = %s", (meeting_id,))
            row = cur.fetchone()
            if row and row[0] != _STANDARD_NO_MATTER_SUMMARY:
                return row[0]
    except Exception:
        pass

    non_routine = [i for i in items if not i.get("isRoutine")]
    routine_count = sum(1 for i in items if i.get("isRoutine"))

    if not items:
        agenda_text = _fetch_agenda_text(meeting_id)
        if agenda_text:
            summary = _summary_from_agenda(body, date_str, agenda_text)
            matter_ids = _extract_matter_ids_from_text(agenda_text)
            if matter_ids:
                logger.info("[elms] meeting %s agenda mentions %d matter IDs: %s",
                            meeting_id, len(matter_ids), matter_ids)
                link_agenda_matters(meeting_id, body, date_str, matter_ids)
        else:
            # PDF unavailable — fall back to any matters already linked via meeting_items
            linked_items = _items_from_meeting_items_table(meeting_id)
            if linked_items:
                logger.info("[elms] meeting %s: PDF unavailable, generating summary from %d linked matters",
                            meeting_id, len(linked_items))
                linked_non_routine = [i for i in linked_items if not i.get("isRoutine")]
                linked_routine_count = sum(1 for i in linked_items if i.get("isRoutine"))
                if linked_non_routine:
                    nr_titles = "\n".join(
                        f"- {i.get('matterType', 'Item')}: {(i.get('matterTitle') or '')[:120]}"
                        for i in linked_non_routine[:20]
                    )
                    prompt = (
                        f"Summarize this Chicago City Council {body} meeting ({date_str}) in 50 words or fewer. "
                        "This was a subject matter hearing — no final votes were taken. "
                        "Focus on what topics were discussed with specific detail. "
                        f"Mention routine items only as a count ({linked_routine_count} routine items). "
                        "Write at a 5th grade reading level. Do not use bullet points — write 2-3 plain sentences.\n\n"
                        f"Items discussed:\n{nr_titles}\n"
                        f"Routine items: {linked_routine_count}"
                    )
                    try:
                        resp = _claude_create(
                            model=CLAUDE_PRIMARY,
                            max_tokens=150,
                            messages=[{"role": "user", "content": prompt}],
                        )
                        summary = resp.content[0].text.strip()
                    except Exception:
                        summary = _STANDARD_NO_MATTER_SUMMARY
                else:
                    summary = _STANDARD_NO_MATTER_SUMMARY
            else:
                summary = _STANDARD_NO_MATTER_SUMMARY

        try:
            with _db() as conn:
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO meeting_summaries (meeting_id, body, meeting_date, summary)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (meeting_id) DO UPDATE
                         SET summary = EXCLUDED.summary,
                             body = COALESCE(EXCLUDED.body, meeting_summaries.body),
                             meeting_date = COALESCE(EXCLUDED.meeting_date, meeting_summaries.meeting_date)""",
                    (meeting_id, body, date_str, summary)
                )
        except Exception:
            pass
        return summary

    if not non_routine:
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
                    """INSERT INTO meeting_summaries (meeting_id, body, meeting_date, summary)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (meeting_id) DO UPDATE
                         SET summary = EXCLUDED.summary,
                             body = COALESCE(EXCLUDED.body, meeting_summaries.body),
                             meeting_date = COALESCE(EXCLUDED.meeting_date, meeting_summaries.meeting_date)""",
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


def fetch_meeting_files(meeting_id: str) -> list[dict]:
    """Fetch meeting-level file attachments (not matter attachments)."""
    try:
        detail = _elms_get(f"/meeting-agenda/{meeting_id}")
        return detail.get("files") or []
    except Exception:
        return []


def get_meeting_document_summaries(meeting_id: str) -> list[dict]:
    """Return meeting-level file attachments as a plain list (no AI summarization)."""
    files = fetch_meeting_files(meeting_id)
    results = []
    for f in files[:10]:
        url = f.get("path") or f.get("url") or ""
        if not url:
            continue
        name = f.get("fileName") or f.get("name") or "Meeting document"
        results.append({"name": name, "url": url})
    return results


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
