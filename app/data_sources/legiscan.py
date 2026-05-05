import hashlib
import json
import logging
import re
from datetime import datetime, timezone

import requests as _http

from ..claude_client import _claude_create
from ..config import CLAUDE_PRIMARY, LEGISCAN_API_KEY, LEGISCAN_BASE
from ..db import _db

logger = logging.getLogger(__name__)

RELEVANCE_CUTOFF = 4

_il_plain_language_cache: dict[str, str] = {}

_STATUS_CONTEXT: dict[str, str] = {
    "introduced": (
        "This bill has been introduced in the Illinois General Assembly and referred to a committee for review."
    ),
    "in_committee": (
        "This bill is currently under review by the committee listed. "
        "The committee may hold hearings, amend the bill, or vote to advance or table it."
    ),
    "passed_house": (
        "This bill passed the Illinois House of Representatives and is now before the Illinois Senate."
    ),
    "passed_senate": (
        "This bill passed the Illinois Senate and is now before the Illinois House of Representatives."
    ),
    "passed_both": (
        "This bill passed both chambers of the Illinois General Assembly and has been sent to the Governor for signature."
    ),
    "signed": (
        "This bill was signed into law by the Governor and is now Illinois state law."
    ),
    "vetoed": (
        "This bill was vetoed by the Governor. The legislature can attempt an override with a three-fifths majority vote in both chambers."
    ),
    "failed": (
        "This bill did not pass and is no longer active in the current legislative session."
    ),
    "tabled": (
        "This bill has been tabled (postponed indefinitely). It may be brought back at a future session."
    ),
    "enrolled": (
        "This bill has passed both chambers and is being prepared for the Governor's signature."
    ),
}

_BILL_TYPE_DESCRIPTIONS: dict[str, str] = {
    "B":  "A bill proposes new state law or amends existing law. If passed and signed, it has full legal force.",
    "R":  "A resolution expresses the General Assembly's opinion or intent but does not have the force of law.",
    "CR": "A concurrent resolution requires passage by both chambers but does not go to the Governor.",
    "JR": "A joint resolution may propose constitutional amendments or address joint business of both chambers.",
    "CA": "A constitutional amendment proposes a change to the Illinois Constitution and requires voter approval.",
}


def preload_il_plain_language_cache() -> None:
    """Load all cached IL bill plain-language titles from DB into memory at startup."""
    global _il_plain_language_cache
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT bill_id, plain_title FROM il_plain_language_titles")
            rows = cur.fetchall()
            for bill_id, pt in rows:
                _il_plain_language_cache[str(bill_id)] = pt
        print(f"Loaded {len(_il_plain_language_cache)} cached IL plain language titles from DB", flush=True)
    except Exception as exc:
        print(f"Warning: could not preload IL plain language titles: {exc}", flush=True)


def _legiscan_get(op: str, params: dict | None = None) -> dict:
    """HTTP wrapper for Legiscan API — always injects key and op."""
    p = {"key": LEGISCAN_API_KEY, "op": op}
    if params:
        p.update(params)
    resp = _http.get(LEGISCAN_BASE, params=p, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "OK":
        raise ValueError(f"Legiscan error for op={op}: {data}")
    return data


def _get_current_session_id(state: str = "IL") -> int | None:
    """Return the Legiscan session_id for the current Illinois legislative session."""
    try:
        data = _legiscan_get("getSessionList", {"state": state})
        sessions = data.get("sessions", [])
        for session in sessions:
            if session.get("special") == 0:
                return session.get("session_id")
    except Exception as exc:
        logger.warning("Could not fetch Legiscan session list: %s", exc)
    return None


def search_bills(query: str, state: str = "IL") -> list:
    """Search Illinois state bills via Legiscan. Returns a list of bill summary dicts."""
    try:
        data = _legiscan_get("getSearchRaw", {"state": state, "query": query, "page": 1})
        results = data.get("searchresult", {})
        bills = [v for k, v in results.items() if k != "summary" and isinstance(v, dict)]
        return bills
    except Exception as exc:
        logger.warning("Legiscan search failed for %r: %s", query, exc)
        return []


def get_recent_bills(state: str = "IL") -> list:
    """Return bills from the current IL session changed in the last 30 days."""
    session_id = _get_current_session_id(state)
    if not session_id:
        return []
    try:
        data = _legiscan_get("getMasterList", {"id": session_id})
        master = data.get("masterlist", {})
        bills = [v for k, v in master.items() if k != "session" and isinstance(v, dict)]
        # Filter to recently-updated bills (last_action_date within 30 days)
        cutoff = datetime.now(timezone.utc)
        recent = []
        for b in bills:
            try:
                last_action = b.get("last_action_date") or ""
                if last_action:
                    dt = datetime.fromisoformat(last_action)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if (cutoff - dt).days <= 30:
                        recent.append(b)
            except Exception:
                pass
        return recent
    except Exception as exc:
        logger.warning("Legiscan getMasterList failed: %s", exc)
        return []


def get_bill_detail(bill_id: int | str) -> dict | None:
    """Fetch full bill detail from Legiscan. Uses il_bill_detail_cache (1-hour TTL for active bills)."""
    bid = str(bill_id)

    # Check DB cache
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT data, cached_at, status FROM il_bill_detail_cache WHERE bill_id = %s",
                (bid,)
            )
            row = cur.fetchone()
            if row:
                data, cached_at, status = row
                terminal = status and any(
                    s in (status or "").lower()
                    for s in ("signed", "vetoed", "failed", "tabled", "enacted", "dead")
                )
                age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
                if terminal or age_hours < 1:
                    return data
    except Exception:
        pass

    try:
        resp = _legiscan_get("getBill", {"id": bill_id})
        bill = resp.get("bill", {})
    except Exception as exc:
        logger.warning("Legiscan getBill failed for id=%s: %s", bill_id, exc)
        return None

    bill = enrich_bill(bill)

    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO il_bill_detail_cache (bill_id, status, data) "
                "VALUES (%s, %s, %s) ON CONFLICT (bill_id) DO UPDATE "
                "SET status = EXCLUDED.status, data = EXCLUDED.data, cached_at = NOW()",
                (bid, bill.get("status"), json.dumps(bill))
            )
    except Exception:
        pass

    return bill


def plain_language_titles(bills: list) -> dict:
    """Translate formal IL bill titles to plain English via Claude. Memory + DB cached."""
    uncached = [b for b in bills if str(b.get("bill_id", "")) not in _il_plain_language_cache]

    if uncached:
        try:
            ids = [str(b["bill_id"]) for b in uncached if b.get("bill_id")]
            with _db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT bill_id, plain_title FROM il_plain_language_titles WHERE bill_id = ANY(%s)",
                    (ids,)
                )
                for bill_id, pt in cur.fetchall():
                    _il_plain_language_cache[str(bill_id)] = pt
        except Exception:
            pass

    still_uncached = [b for b in bills if str(b.get("bill_id", "")) not in _il_plain_language_cache]
    if still_uncached:
        items = "\n".join(
            f'{{"id": "{b.get("bill_id")}", "title": "{(b.get("title") or b.get("description") or "").replace(chr(34), "")[:150]}"}}'
            for b in still_uncached
        )
        prompt = (
            "Translate these Illinois state legislature bill titles into plain English. "
            "Each translation should be 1 sentence, ≤15 words, no legal jargon, written for a general audience. "
            'Return ONLY a JSON object mapping each id to its plain English translation. Example: {"12345": "Increases the minimum wage to $16 per hour"}\n\n'
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
                for bid, plain in translations.items():
                    _il_plain_language_cache[str(bid)] = plain
                if translations:
                    orig_titles = {
                        str(b.get("bill_id")): (b.get("title") or b.get("description") or "")[:500]
                        for b in still_uncached
                    }
                    try:
                        with _db() as conn:
                            cur = conn.cursor()
                            for bid, plain in translations.items():
                                cur.execute(
                                    "INSERT INTO il_plain_language_titles (bill_id, original_title, plain_title) "
                                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                    (str(bid), orig_titles.get(str(bid)), plain)
                                )
                    except Exception:
                        pass
        except Exception:
            pass

    return {
        str(b.get("bill_id")): _il_plain_language_cache.get(str(b.get("bill_id")))
        for b in bills
    }


def _bill_document_summary(url: str, doc_type: str = "") -> str | None:
    """Download a Legiscan bill text/amendment PDF, summarize at 5th-grade level. DB-cached by URL hash."""
    from io import BytesIO
    url_hash = hashlib.md5(url.encode()).hexdigest()

    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT summary FROM il_document_summaries WHERE url_hash = %s", (url_hash,))
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
        "Summarize this Illinois state government document in 2-3 sentences "
        "at a 5th grade reading level. Describe what is being proposed or considered — "
        "do not assume it was approved or passed. Be specific about what it involves, "
        "who it affects, and where (if mentioned)."
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

    if len(text) < 50:
        return None

    try:
        ai_resp = _claude_create(
            model=CLAUDE_PRIMARY,
            max_tokens=200,
            messages=[{"role": "user", "content": _SUMMARY_PROMPT + "\n\n" + text}],
        )
        summary = ai_resp.content[0].text.strip()
    except Exception:
        return None

    if summary:
        try:
            with _db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO il_document_summaries (url_hash, url, doc_type, summary) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (url_hash, url, doc_type or None, summary)
                )
        except Exception:
            pass

    return summary


def enrich_bill(bill: dict) -> dict:
    """Add statusContext, typeDescription, document summaries, and whatCanYouDo to a Legiscan bill dict."""
    # Bill type description
    bill_type = (bill.get("bill_type") or bill.get("type") or "").upper()
    for key, desc in _BILL_TYPE_DESCRIPTIONS.items():
        if bill_type.startswith(key):
            bill["typeDescription"] = desc
            break

    # Status context
    status_raw = (bill.get("status") or "").lower()
    progress = bill.get("progress") or []
    has_governor = any(p.get("event") == 9 for p in progress)   # 9 = Governor
    passed_house = any(p.get("event") == 4 for p in progress)   # 4 = Passed House
    passed_senate = any(p.get("event") == 3 for p in progress)  # 3 = Passed Senate

    if "signed" in status_raw or (has_governor and "veto" not in status_raw):
        bill["statusContext"] = _STATUS_CONTEXT["signed"]
    elif "veto" in status_raw:
        bill["statusContext"] = _STATUS_CONTEXT["vetoed"]
    elif "enroll" in status_raw or (passed_house and passed_senate):
        bill["statusContext"] = _STATUS_CONTEXT["passed_both"]
    elif passed_house and not passed_senate:
        bill["statusContext"] = _STATUS_CONTEXT["passed_house"]
    elif passed_senate and not passed_house:
        bill["statusContext"] = _STATUS_CONTEXT["passed_senate"]
    elif "fail" in status_raw or "dead" in status_raw:
        bill["statusContext"] = _STATUS_CONTEXT["failed"]
    elif "table" in status_raw:
        bill["statusContext"] = _STATUS_CONTEXT["tabled"]
    elif "committee" in status_raw:
        bill["statusContext"] = _STATUS_CONTEXT["in_committee"]
    else:
        bill["statusContext"] = _STATUS_CONTEXT["introduced"]

    # PDF document summaries (bill text documents)
    documents = bill.get("texts") or []
    for doc in documents[:2]:
        url = doc.get("state_link") or doc.get("url") or ""
        if url:
            summary = _bill_document_summary(url, doc_type=doc.get("type", "bill_text"))
            if summary:
                doc["summary"] = summary

    # What can you do
    what_can_you_do = []
    is_active = not any(
        s in status_raw for s in ("signed", "vetoed", "failed", "dead", "enacted")
    )
    if is_active:
        sponsors = bill.get("sponsors") or []
        primary_sponsor = next(
            (s.get("name") for s in sponsors if s.get("sponsor_type_id") == 1), None
        )
        if primary_sponsor:
            what_can_you_do.append({
                "action": f"Contact the primary sponsor: {primary_sponsor}",
                "detail": "The bill's sponsor introduced this legislation and can provide updates on its progress.",
                "link": "https://www.ilga.gov/house/",
                "linkText": "Find your Illinois state representative",
            })
        what_can_you_do.append({
            "action": "Contact your state senator",
            "detail": "Your state senator can vote on this bill and advocate for or against it in committee.",
            "link": "https://www.ilga.gov/senate/",
            "linkText": "Find your Illinois state senator",
        })
        what_can_you_do.append({
            "action": "Contact your state representative",
            "detail": "Your state representative can vote on this bill in the Illinois House.",
            "link": "https://www.ilga.gov/house/",
            "linkText": "Find your Illinois state representative",
        })

    if what_can_you_do:
        bill["whatCanYouDo"] = what_can_you_do

    return bill


def claude_rerank(query: str, bills: list) -> list:
    """Rerank Legiscan search results by relevance to query using Claude."""
    if not bills:
        return bills
    numbered = "\n".join(
        f"{i+1}. [{b.get('bill_number', b.get('bill_id', ''))}] {(b.get('title') or b.get('description') or '')[:120]}"
        for i, b in enumerate(bills)
    )
    prompt = (
        f"User query: {query}\n\n"
        "Score each Illinois state legislature bill by relevance to the query (1=irrelevant, 10=exact match). "
        "Return ONLY a JSON array of objects ordered from most to least relevant. "
        'Format: [{"id": "<bill_id>", "score": 8}, ...]. No other text.\n\n'
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
            return bills
        scored = json.loads(match.group())
        id_to_bill = {str(b.get("bill_id")): b for b in bills}
        reranked = []
        seen = set()
        for item in scored:
            bid = str(item.get("id"))
            score = item.get("score", 0)
            if bid in id_to_bill and score >= RELEVANCE_CUTOFF:
                reranked.append(id_to_bill[bid])
                seen.add(bid)
        reranked += [b for b in bills if str(b.get("bill_id")) not in seen]
        return reranked
    except Exception:
        return bills
