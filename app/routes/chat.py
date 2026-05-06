import json
import logging
import re
import time

import anthropic
import numpy as np
from flask import Blueprint, jsonify, request

from ..claude_client import _claude_create
from ..config import CLAUDE_PRIMARY, TOP_K, SCORE_THRESHOLD
from ..data_sources import rag as _rag
from ..data_sources import CONTEXT_SOURCES
from ..data_sources import socrata as _socrata
from ..data_sources.socrata import (
    DATASET_HAS_LOCATION, DATASETS,
    _check_location_in_query, _extract_columns_from_where, _translate_community_areas_in_where,
    parse_intent, query_socrata,
)
from ..data_sources import illinois_socrata as _il_socrata
from ..data_sources.illinois_socrata import (
    ILLINOIS_DATASETS, query_illinois_socrata,
)
from ..prompts import (
    DISCLAIMER_TEMPLATE, OPEN_DATA_DISCLAIMER,
    SYSTEM_PROMPT_DATA, SYSTEM_PROMPT_DOMAIN,
)
from ..session_store import (
    get_last_intent, log_data_query, log_feedback,
    log_source_debug, save_last_intent, upsert_turn,
)

logger = logging.getLogger(__name__)
bp = Blueprint("chat", __name__)

LANG_NAMES = {
    "en": "English", "es": "Spanish", "pl": "Polish",
    "zh": "Chinese (Simplified)", "ar": "Arabic",
    "tl": "Tagalog", "hi": "Hindi",
}

_DEFAULTS = (False, "", None)


def _merge_intents(base: dict, latest: dict) -> dict:
    merged = {**base}
    for key, value in latest.items():
        base_value = base.get(key)
        if base_value not in _DEFAULTS and value in _DEFAULTS:
            continue
        merged[key] = value
    if merged.get("has_location") and merged.get("location_phrase"):
        merged["is_citywide"] = False
    return merged


@bp.route("/chat", methods=["POST"])
def chat():
    logger.info("[chat] handler entered")
    req            = request.get_json(silent=True) or {}
    question       = req.get("question", "").strip()
    lang           = req.get("lang", "en").strip() or "en"
    history        = req.get("history", [])
    session_id     = req.get("session_id", "")[:64]
    clarify_count  = int(req.get("clarify_count", 0))
    pending_intent = req.get("pending_intent") or None

    logger.info("[chat] question=%r lang=%r session=%r turns=%d pending=%s",
                question[:80], lang, session_id, len(history), bool(pending_intent))

    if not question:
        return jsonify({"error": "No question provided"}), 400

    lang_name = LANG_NAMES.get(lang, "English")
    t0 = time.monotonic()

    def elapsed():
        return f"{time.monotonic() - t0:.2f}s"

    try:
        _prev_stored = get_last_intent(session_id)

        # Intent resolution
        if pending_intent and clarify_count > 0:
            latest_intent = parse_intent(question, history)
            intent = _merge_intents(pending_intent, latest_intent)
            if not pending_intent.get("has_location"):
                loc_check = _check_location_in_query(question)
                if loc_check["status"] == "valid":
                    intent["has_location"] = True
                    intent["location_phrase"] = loc_check["name"]
                    intent["is_citywide"] = False
                    logger.info("[chat] location from clarification: %s", loc_check)
                elif loc_check["status"] == "citywide":
                    intent["has_location"] = True
                    intent["is_citywide"] = True
                    intent["location_phrase"] = "all of Chicago"
            logger.info("[chat] +%s merged pending_intent: %s", elapsed(), intent)
        else:
            logger.info("[chat] +%s parsing intent", elapsed())
            try:
                intent = parse_intent(question, history)
            except anthropic.APIStatusError as exc:
                if exc.status_code == 529:
                    return jsonify({"type": "error", "message": "This service is briefly overloaded. Please try again in a moment."})
                raise
            logger.info("[chat] intent: %s", intent)

            if intent.get("is_data_query") and _prev_stored:
                if _prev_stored.get("dataset") == intent.get("dataset"):
                    intent = _merge_intents(_prev_stored, intent)
                    logger.info("[chat] +%s merged with stored intent: %s", elapsed(), intent)

        use_data_tool        = bool(intent.get("is_data_query"))
        active_system_prompt = SYSTEM_PROMPT_DATA if use_data_tool else SYSTEM_PROMPT_DOMAIN
        resolved_area_num    = None

        # Pre-flight checks for data queries
        if use_data_tool:
            dataset         = intent.get("dataset")
            has_time        = intent.get("has_time", False)
            has_location    = intent.get("has_location", False)
            is_citywide     = intent.get("is_citywide", False)
            location_phrase = intent.get("location_phrase", "")
            loc_supported   = DATASET_HAS_LOCATION.get(dataset, True)

            if not has_time:
                msg = "What time period are you asking about? (e.g., 2024, last year, January–June 2025)"
                upsert_turn(session_id, lang,
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": msg, "type": "clarification", "sources": []},
                )

            if loc_supported and not has_location:
                msg = "Which neighborhood in Chicago are you asking about, or would you like data for all of Chicago?"
                upsert_turn(session_id, lang,
                    {"role": "user", "content": question},
                    {"role": "assistant", "content": msg, "type": "clarification", "sources": []},
                )
                save_last_intent(session_id, intent)
                return jsonify({"type": "clarification", "answer": msg, "sources": [], "pending_intent": intent})

            if has_location and not is_citywide:
                loc = _check_location_in_query(location_phrase or question)
                logger.info("[chat] location check: %s", loc)
                if loc["status"] == "invalid":
                    areas_list = ", ".join(sorted(_socrata.COMMUNITY_AREA_BY_NUM.values()))
                    msg = (
                        f"'{loc['mention']}' is not a Chicago community area. "
                        f"Please choose one of the 77 official community areas:\n\n{areas_list}"
                    )
                    upsert_turn(session_id, lang,
                        {"role": "user", "content": question},
                        {"role": "assistant", "content": msg, "type": "clarification", "sources": []},
                    )
                    intent_without_location = {**intent, "has_location": False, "location_phrase": ""}
                    save_last_intent(session_id, intent_without_location)
                    return jsonify({"type": "clarification", "answer": msg, "sources": [], "pending_intent": intent_without_location})
                elif loc["status"] == "valid":
                    resolved_area_num = loc["num"]
                    logger.info("[chat] resolved community area: %s → %d", loc["name"], loc["num"])

        # Embed + RAG search
        logger.info("[chat] +%s embedding question", elapsed())
        rag_source = next((s for s in CONTEXT_SOURCES), None)
        embedding = rag_source.embed(question) if rag_source else []

        logger.info("[chat] +%s querying pgvector", elapsed())
        context = rag_source.fetch(question, embedding) if rag_source else ""
        sources = getattr(rag_source, "_last_sources", [])

        # Build user message
        if pending_intent and clarify_count > 0:
            original_q = next(
                (t.get("content", "") for t in history if t.get("role") == "user"),
                question,
            )
            effective_question = (
                f"{original_q}\n[User provided: {question}]"
                if original_q and original_q != question
                else question
            )
        else:
            effective_question = question

        user_content = (
            f"Respond in {lang_name}.\n\n"
            f"Context from chicago.gov:\n\n{context}\n\n"
            f"Question: {effective_question}"
        )
        if resolved_area_num is not None:
            area_name = _socrata.COMMUNITY_AREA_BY_NUM[resolved_area_num]
            user_content += (
                f"\n\n[LOCATION NOTE: '{area_name}' = community_area {resolved_area_num}. "
                f"Use community_area='{resolved_area_num}' (text, not bare int) in any Socrata WHERE clause.]"
            )
        date_col_override = intent.get("date_column", "")
        if date_col_override:
            user_content += (
                f"\n\n[DATE COLUMN NOTE: The user has specified to use '{date_col_override}' for date filtering. "
                f"Use this column in your WHERE clause. Tell the user which column you are using.]"
            )
        if clarify_count >= 1:
            user_content += (
                "\n\nNOTE: You have already asked 1 clarifying question in a row. "
                "Do NOT ask another clarifying question. "
                "Answer with what you can from the context above, or say "
                "'I don't know' and suggest the most relevant links from the context."
            )
        if use_data_tool and dataset and dataset in _socrata.SCHEMA_CACHE:
            col_list = ", ".join(
                f"{c['fieldName']} ({c['dataTypeName']})" for c in _socrata.SCHEMA_CACHE[dataset]
            )
            user_content += (
                f"\n\n[SCHEMA NOTE: The {dataset} dataset has these columns: {col_list}. "
                f"Use ONLY these column names in your WHERE and SELECT clauses.]"
            )
            group_by_col = intent.get("group_by", "")
            if group_by_col:
                user_content += (
                    f"\n\n[GROUP BY NOTE: The user wants results broken down by '{group_by_col}'. "
                    f"Set select to '{group_by_col}, count(*) AS total' and group to '{group_by_col}'.]"
                )
            _prev_query = _prev_stored.get("_last_query") if _prev_stored else None
            if _prev_query and _prev_query.get("dataset") == dataset:
                _pq = _prev_query
                _prev_sql = f"SELECT {_pq['select']} FROM {_pq['dataset']}"
                if _pq.get("where"):
                    _prev_sql += f" WHERE {_pq['where']}"
                if _pq.get("group"):
                    _prev_sql += f" GROUP BY {_pq['group']}"
                user_content += (
                    f"\n\n[PREVIOUS QUERY: {_prev_sql}]"
                    "\n[QUERY CONSISTENCY: Base your new query on the PREVIOUS QUERY above. "
                    "Only change the parts the user explicitly updated. "
                    "Reuse the same column names for the same concepts.]"
                )

        messages = []
        for turn in history:
            role    = turn.get("role", "")
            content = turn.get("content", "")
            if role not in ("user", "assistant") or not content:
                continue
            if isinstance(content, list):
                if role == "user" and all(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content if isinstance(b, dict)
                ):
                    continue
                text_parts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                content = " ".join(text_parts).strip()
                if not content:
                    continue
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_content})

        data_query_meta = None
        tool_result     = None

        all_tools = _socrata.SOCRATA_TOOLS + _il_socrata.ILLINOIS_SOCRATA_TOOLS
        logger.info("[chat] +%s calling Claude  data_tool=%s", elapsed(), use_data_tool)
        try:
            message = _claude_create(
                model      = CLAUDE_PRIMARY,
                max_tokens = 400,
                system     = active_system_prompt,
                tools      = all_tools,
                messages   = messages,
            )
        except anthropic.APIStatusError as exc:
            if exc.status_code == 529:
                return jsonify({"type": "error", "message": "The AI service is briefly overloaded. Please try again in a moment."})
            if exc.status_code == 400:
                return jsonify({"type": "limit", "subtype": "context", "answer": "This tool is still being built and can only remember so much of a conversation. Want to see it improve? Tap \U0001f44d below!"})
            raise

        logger.info("[chat] +%s first Claude call done (stop=%s)", elapsed(), message.stop_reason)

        if message.stop_reason == "tool_use":
            tool_use_blocks = [b for b in message.content if b.type == "tool_use"]
            tool_results_content = []
            fallback_needed = False
            fallback_error = ""
            last_good_meta = None

            for tool_use_block in tool_use_blocks:
                tool_name  = tool_use_block.name
                tool_input = tool_use_block.input
                dataset = tool_input.get("dataset", "")
                where   = tool_input.get("where") or ""
                select  = tool_input.get("select") or "count(*) AS total"
                group   = tool_input.get("group") or ""
                group   = re.sub(r'(?i)^\s*group\s+by\s+', '', group).strip()
                is_illinois = (tool_name == "query_illinois_data")

                if group and not group.startswith("coalesce("):
                    coalesced = f"coalesce({group}, 'Unknown')"
                    parts = [p.strip() for p in select.split(',')]
                    new_parts = []
                    for part in parts:
                        if re.match(r'^' + re.escape(group) + r'\s*$', part, re.IGNORECASE):
                            new_parts.append(f"coalesce({group}, 'Unknown') AS {group}")
                        else:
                            new_parts.append(part)
                    select = ', '.join(new_parts)
                    group = coalesced

                if where:
                    normalized = re.sub(r'"(\d{4}-\d{2}-\d{2}(?:T[^"]*)?)"', r"'\1'", where)
                    normalized = re.sub(r'\s+(?:AND|OR)\s*$', '', normalized, flags=re.IGNORECASE).strip()
                    if normalized != where:
                        where = normalized

                # City community area translation only applies to Chicago datasets
                if not is_illinois and where and _socrata.COMMUNITY_AREA_BY_NAME:
                    translated = _translate_community_areas_in_where(where)
                    if translated != where:
                        where = translated

                is_count_query  = "count(" in select.lower()
                is_listing_query = not is_count_query and not group
                is_group_query  = bool(group)
                row_limit = 1 if (is_count_query and not group) else (200 if is_group_query else 50)

                logger.info("[tool_use] tool=%s dataset=%s where=%r select=%r group=%r limit=%d",
                            tool_name, dataset, where, select, group, row_limit)

                if is_illinois:
                    tool_result = query_illinois_socrata(dataset=dataset, where=where or None, select=select, group=group, limit=row_limit)
                else:
                    tool_result = query_socrata(dataset=dataset, where=where or None, select=select, group=group, limit=row_limit)

                if is_listing_query and isinstance(tool_result, list):
                    count_result = query_socrata(dataset=dataset, where=where or None, select="count(*) AS total", limit=1)
                    total_count = None
                    if isinstance(count_result, list) and count_result:
                        try:
                            total_count = int(count_result[0].get("total", 0))
                        except (ValueError, TypeError):
                            pass
                    if total_count is not None:
                        tool_result = {
                            "total_count": total_count,
                            "showing": len(tool_result),
                            "note": "Only the first 50 results are shown.",
                            "results": tool_result,
                        }

                if isinstance(tool_result, dict) and "results" in tool_result:
                    records = len(tool_result["results"])
                elif isinstance(tool_result, list):
                    records = len(tool_result)
                else:
                    records = None

                if isinstance(tool_result, dict) and "error" in tool_result:
                    error_msg = tool_result.get("error", "")
                    if "400" in error_msg:
                        return jsonify({"type": "error", "message": "Sorry, the data query failed. Please try again."})
                    fallback_needed = True
                    fallback_error = error_msg
                    tool_results_content.append({
                        "type":        "tool_result",
                        "tool_use_id": tool_use_block.id,
                        "content":     json.dumps(tool_result),
                    })
                else:
                    last_good_meta = {
                        "dataset":          dataset,
                        "where":            where,
                        "select":           select,
                        "group":            group,
                        "records_returned": records or 0,
                    }
                    extracted = _extract_columns_from_where(where)
                    for col_key, col_val in extracted.items():
                        if not intent.get(col_key):
                            intent[col_key] = col_val
                    tool_results_content.append({
                        "type":        "tool_result",
                        "tool_use_id": tool_use_block.id,
                        "content":     json.dumps(tool_result),
                    })

            if fallback_needed:
                try:
                    message = _claude_create(
                        model      = CLAUDE_PRIMARY,
                        max_tokens = 400,
                        system     = active_system_prompt,
                        tool_choice= {"type": "none"},
                        tools      = all_tools,
                        messages   = [messages[-1]],
                    )
                except anthropic.APIStatusError as exc:
                    if exc.status_code == 529:
                        return jsonify({"type": "error", "message": "The AI service is briefly overloaded. Please try again in a moment."})
                    raise
            else:
                if last_good_meta:
                    data_query_meta = last_good_meta
                messages.append({"role": "assistant", "content": message.content})
                messages.append({"role": "user", "content": tool_results_content})
                try:
                    message = _claude_create(
                        model      = CLAUDE_PRIMARY,
                        max_tokens = 400,
                        system     = active_system_prompt,
                        tools      = all_tools,
                        messages   = messages,
                    )
                except anthropic.APIStatusError as exc:
                    if exc.status_code == 529:
                        return jsonify({"type": "error", "message": "The AI service is briefly overloaded. Please try again in a moment."})
                    raise
                logger.info("[chat] +%s second Claude call done", elapsed())

        if message.stop_reason == "max_tokens":
            return jsonify({
                "type"   : "limit",
                "subtype": "tokens",
                "answer" : "This tool is still being built and ran into a limit. Want to see it get better? Tap \U0001f44d below!",
                "sources": [],
            })

        text_block = next((b for b in message.content if b.type == "text"), None)
        raw = text_block.text.strip() if text_block else ""

        if clarify_count < 2 and raw.upper().startswith("CLARIFY:"):
            clarification = raw[len("CLARIFY:"):].strip()
            upsert_turn(session_id, lang,
                {"role": "user",      "content": question},
                {"role": "assistant", "content": clarification, "type": "clarification", "sources": []},
            )
            save_last_intent(session_id, intent)
            return jsonify({"type": "clarification", "answer": clarification, "sources": [], "pending_intent": intent})

        answer_text = raw
        used_urls: set[str] = set()
        lines = raw.splitlines()
        for i, line in enumerate(lines):
            if line.strip().upper().startswith("SOURCES:"):
                payload = line.split(":", 1)[1].strip()
                if payload.lower() != "none":
                    for part in payload.split(","):
                        url = part.strip().rstrip(".")
                        if url:
                            used_urls.add(url)
                answer_text = "\n".join(lines[:i]).strip()
                break

        fallback_used = False
        if used_urls:
            filtered_sources = [s for s in sources if s["url"] in used_urls]
            if not filtered_sources:
                filtered_sources = [s for s in sources if any(u in s["url"] for u in used_urls)]
        else:
            filtered_sources = []

        if not filtered_sources and sources:
            filtered_sources = sources
            fallback_used = True

        log_source_debug(session_id, question,
            retrieved_urls=[s["url"] for s in sources],
            used_urls=used_urls,
            filtered_urls=[s["url"] for s in filtered_sources],
            fallback_used=fallback_used,
        )
        if data_query_meta:
            log_data_query(session_id, question,
                data_query_meta["dataset"], data_query_meta["where"],
                data_query_meta["select"],  data_query_meta["records_returned"],
                tool_result,
            )

        assistant_turn = {
            "role": "assistant", "content": answer_text, "type": "answer",
            "sources": [s["url"] for s in filtered_sources],
            "used_urls": sorted(used_urls),
            "fallback_used": fallback_used,
        }
        if data_query_meta:
            assistant_turn["data_query"] = data_query_meta
        upsert_turn(session_id, lang,
            {"role": "user", "content": question},
            assistant_turn,
        )
        intent_to_save = {**intent, "_last_query": data_query_meta} if data_query_meta else None
        save_last_intent(session_id, intent_to_save)
        logger.info("[chat] +%s done", elapsed())

        if data_query_meta:
            q_dataset = data_query_meta["dataset"]
            if q_dataset in ILLINOIS_DATASETS:
                dataset_id = ILLINOIS_DATASETS.get(q_dataset, "")
                filtered_sources = [
                    {"title": "Illinois Open Data Portal", "url": "https://data.illinois.gov"},
                    {"title": f"Dataset: {q_dataset} ({dataset_id})",
                     "url": f"https://data.illinois.gov/resource/{dataset_id}"},
                ]
            else:
                dataset_id = DATASETS.get(q_dataset, "")
                filtered_sources = [
                    {"title": "Chicago Open Data Portal", "url": "https://data.cityofchicago.org"},
                    {"title": f"Dataset: {q_dataset} ({dataset_id})",
                     "url": f"https://data.cityofchicago.org/resource/{dataset_id}"},
                ]

        resp_body = {
            "type"       : "answer",
            "answer"     : answer_text,
            "sources"    : filtered_sources,
            "scrape_date": _rag.SCRAPE_DATE,
            "disclaimer" : OPEN_DATA_DISCLAIMER if data_query_meta else DISCLAIMER_TEMPLATE.format(date=_rag.SCRAPE_DATE),
        }
        if data_query_meta:
            resp_body["data_query"] = data_query_meta
        return jsonify(resp_body)

    except anthropic.BadRequestError as exc:
        logger.info("Bad Request Error: %s", exc)
        return jsonify({
            "type"   : "limit",
            "subtype": "context",
            "answer" : "This tool is still being built and can only remember so much of a conversation. Want to see it improve? Tap \U0001f44d below!",
            "sources": [],
        })
    except Exception as exc:
        logger.error("[chat] unexpected error: %s", exc, exc_info=True)
        return jsonify({"type": "error", "message": "Something went wrong. Please try again."}), 500


@bp.route("/feedback", methods=["POST"])
def feedback():
    data          = request.get_json(silent=True) or {}
    feedback_type = data.get("type", "").strip()
    note          = data.get("note", "").strip()
    session_id    = data.get("session_id", "")[:64]

    if feedback_type not in ("up", "down"):
        return jsonify({"ok": False, "error": "Invalid feedback type."}), 400

    log_feedback(session_id, feedback_type, note)
    return jsonify({"ok": True})
