import csv
import json
import logging
import re
import ssl
import urllib.parse
import urllib.request
from pathlib import Path

import certifi
import anthropic

from ..config import CLAUDE_PRIMARY
from ..claude_client import _claude_create

logger = logging.getLogger(__name__)

SOCRATA_BASE = "https://data.cityofchicago.org/resource"

DATASETS = {
    "business_licenses": "r5kz-chrr",
    "building_permits":  "ydr8-5enu",
    "crime":             "ijzp-q8t2",
    "311_requests":      "v6vf-nfxy",
}

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
import os
SOCRATA_APP_TOKEN = os.environ.get("SOCRATA_APP_TOKEN", "")

DATASET_HAS_LOCATION: dict[str, bool] = {
    "crime": True,
    "building_permits": True,
    "business_licenses": True,
    "311_requests": True,
}

DATASET_LABELS: dict[str, str] = {
    "crime": "crime incidents",
    "building_permits": "building permits",
    "business_licenses": "business licenses",
    "311_requests": "311 service requests",
}

COMMUNITY_AREA_BY_NAME: dict[str, int] = {}
COMMUNITY_AREA_BY_NUM:  dict[int, str] = {}

_HERE = Path(__file__).parent.parent.parent  # project root

SCHEMA_CACHE: dict[str, list] = {}
SCHEMA_CACHE_PATH = _HERE / "dataset_schemas.json"

SOCRATA_TOOLS: list = []  # rebuilt in load_resources() after community areas are loaded

INTENT_TOOL = {
    "name": "parse_data_query_intent",
    "description": (
        "Extract structured intent from a user question. "
        "Identify whether the question asks for quantitative data from one of the four "
        "Chicago Open Data Portal datasets, and extract the time period and location if present."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_data_query": {
                "type": "boolean",
                "description": (
                    "True ONLY if the user is asking a quantitative question "
                    "(how many, count, total, rate, trend, etc.) about one of these four topics: "
                    "crime or arrests, building permits, business licenses, or 311 service requests. "
                    "False for questions about schools, parks, transit, health, or any general info."
                ),
            },
            "dataset": {
                "type": "string",
                "enum": ["crime", "building_permits", "business_licenses", "311_requests"],
                "description": "The matching dataset key. Include only when is_data_query is true.",
            },
            "has_time": {
                "type": "boolean",
                "description": (
                    "True if the question specifies a time period — a year, month, date range, "
                    "or relative phrase like 'last year', 'this month', 'since 2022', etc."
                ),
            },
            "time_phrase": {
                "type": "string",
                "description": "The time period as the user stated it. Empty string if none.",
            },
            "date_column": {
                "type": "string",
                "description": (
                    "If the user explicitly names a specific date column to use for filtering, "
                    "capture it here. Empty string if the user did not specify a column."
                ),
            },
            "location_column": {
                "type": "string",
                "description": (
                    "If the user explicitly names a specific location column to filter on, "
                    "capture it here. Empty string if the user did not specify a column."
                ),
            },
            "has_location": {
                "type": "boolean",
                "description": (
                    "True if the question names a specific Chicago location (neighborhood, "
                    "community area, ward) OR explicitly says 'all of Chicago', 'citywide', etc."
                ),
            },
            "location_phrase": {
                "type": "string",
                "description": "The location as the user stated it. Empty string if none.",
            },
            "is_citywide": {
                "type": "boolean",
                "description": (
                    "True if the user wants data for all of Chicago with no specific neighborhood."
                ),
            },
            "group_by": {
                "type": "string",
                "description": (
                    "The column to group/break down results by, if the user asks for a breakdown. "
                    "Use the exact Socrata column name, e.g. 'community_area' or 'ward'. "
                    "Empty string if no grouping is requested."
                ),
            },
        },
        "required": ["is_data_query", "has_time", "has_location", "is_citywide"],
    },
}

_LOCATION_SKIP_WORDS = frozenset({
    "chicago", "illinois", "il", "city", "the", "a", "an",
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
    "2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026", "2027",
    "recent", "last", "this", "past", "current", "total", "all", "any",
})

_LOCATION_PREP_RE = re.compile(
    r"\b(?:in|around|near|within|at)\s+(?:the\s+)?([A-Za-z][A-Za-z\s'\-]{1,35})",
    re.IGNORECASE,
)

_CITYWIDE_RE = re.compile(
    r"\b(all of chicago|citywide|city-wide|whole city|entire city|all chicago|no specific|everywhere)\b",
    re.IGNORECASE,
)


def load_community_areas() -> None:
    global COMMUNITY_AREA_BY_NAME, COMMUNITY_AREA_BY_NUM
    matches = sorted(_HERE.glob("Boundaries_-_Community_Areas*.csv"))
    if not matches:
        print("WARNING: No community areas CSV found — neighborhood name lookup disabled.", flush=True)
        return
    csv_path = matches[-1]
    print(f"Loading community areas from {csv_path.name}...", flush=True)
    by_name: dict[str, int] = {}
    by_num:  dict[int, str] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            num  = int(row["AREA_NUMBE"])
            name = row["COMMUNITY"].strip().title()
            by_name[name.lower()] = num
            by_num[num] = name
    COMMUNITY_AREA_BY_NAME = by_name
    COMMUNITY_AREA_BY_NUM  = by_num
    print(f"  Loaded {len(by_num)} community areas.", flush=True)


def load_dataset_schemas() -> None:
    global SCHEMA_CACHE
    if SCHEMA_CACHE_PATH.exists():
        with open(SCHEMA_CACHE_PATH) as f:
            SCHEMA_CACHE = json.load(f)
        print(f"Loaded dataset schemas from cache ({len(SCHEMA_CACHE)} datasets).", flush=True)
        return
    schemas: dict[str, list] = {}
    for key, dataset_id in DATASETS.items():
        if key in schemas:
            continue
        try:
            print("Fetching dataset schemas from Socrata...", flush=True)
            url = f"https://data.cityofchicago.org/api/views/{dataset_id}.json"
            schema_req = urllib.request.Request(url)
            if SOCRATA_APP_TOKEN:
                schema_req.add_header("X-App-Token", SOCRATA_APP_TOKEN)
            with urllib.request.urlopen(schema_req, timeout=15, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode())
            schemas[key] = [
                {"fieldName": col["fieldName"], "dataTypeName": col["dataTypeName"]}
                for col in data.get("columns", [])
                if "hidden" not in col.get("flags", [])
            ]
            print(f"  {key}: {len(schemas[key])} columns", flush=True)
        except Exception as e:
            print(f"  WARNING: could not fetch schema for {key}: {e}", flush=True)
    SCHEMA_CACHE = schemas
    with open(SCHEMA_CACHE_PATH, "w") as f:
        json.dump(schemas, f, indent=2)
    print(f"Dataset schemas saved to {SCHEMA_CACHE_PATH.name}", flush=True)


def build_socrata_tools() -> list:
    if COMMUNITY_AREA_BY_NUM:
        areas_str = "; ".join(
            f"{name}={num}" for num, name in sorted(COMMUNITY_AREA_BY_NUM.items())
        )
        community_area_note = (
            "community_area is a string.\n"
            f"Valid community areas: {areas_str}.\n"
            "If the user names a place that is NOT in this list, do not guess a number — "
            "tell the user that location is not a Chicago community area."
        )
    else:
        community_area_note = (
            "community_area is text (e.g. Loop='32'). "
            "Do NOT filter by neighborhood name string."
        )

    return [
        {
            "name": "query_chicago_data",
            "description": (
                "Query the Chicago Open Data Portal for live statistics. "
                "ONLY call this tool for questions explicitly about one of the four available datasets: "
                "business_licenses, building_permits, crime, 311_requests. "
                "Do NOT call for schools, CPS enrollment, parks, transit, health, or any other topic."
                "Validate all columns in the select statement against dataset schemas."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "dataset": {
                        "type": "string",
                        "enum": ["business_licenses", "building_permits", "crime", "311_requests"],
                        "description": "Which dataset to query",
                    },
                    "where": {
                        "type": "string",
                        "description": (
                            "SODA $where clause using SoQL syntax. Examples:\n"
                            "  date >= '2025-01-01' AND date < '2026-01-01'\n"
                            "  year = '2025' AND primary_type = 'THEFT'\n"
                            f"IMPORTANT for crime/permit datasets: {community_area_note}\n"
                            "Crime date field is 'date'. Use date range for year filtering.\n"
                            "primary_type values are ALL CAPS strings like 'THEFT', 'ASSAULT', 'HOMICIDE'.\n"
                            "MONTH-END RULE: Always use the first day of the NEXT month as the exclusive "
                            "upper bound for a month range. April 2026 = >= '2026-04-01' AND < '2026-05-01'. "
                            "Never use the last day of the month (e.g. < '2026-04-30') — it excludes April 30."
                        ),
                    },
                    "select": {
                        "type": "string",
                        "description": (
                            "SODA $select clause.\n"
                            "HOW MANY questions: use \"count(*) AS total\".\n"
                            "BREAKDOWN questions: use \"<group_col>, count(*) AS total\".\n"
                            "LIST/NAME questions: use specific columns.\n"
                            "Default to \"count(*) AS total\" when the intent is ambiguous."
                        ),
                    },
                    "group": {
                        "type": "string",
                        "description": (
                            "Column name to group by, e.g. \"primary_type\" or \"license_description\". "
                            "Do NOT include the words 'GROUP BY' — just the column name."
                        )
                    }
                },
                "required": ["dataset"],
            },
        }
    ]


def _translate_community_areas_in_where(where: str) -> str:
    for name_lower, num in COMMUNITY_AREA_BY_NAME.items():
        for q in ("'", '"'):
            pattern = re.compile(re.escape(q) + re.escape(name_lower) + re.escape(q), re.IGNORECASE)
            where = pattern.sub(str(num), where)
    return where


def _check_location_in_query(question: str) -> dict:
    if _CITYWIDE_RE.search(question):
        return {"status": "citywide"}
    if not COMMUNITY_AREA_BY_NAME:
        return {"status": "none"}
    q_lower = question.lower()
    for name_lower, num in sorted(COMMUNITY_AREA_BY_NAME.items(), key=lambda x: -len(x[0])):
        if re.search(r"\b" + re.escape(name_lower) + r"\b", q_lower):
            return {"status": "valid", "name": COMMUNITY_AREA_BY_NUM[num], "num": num}
    for m in _LOCATION_PREP_RE.finditer(question):
        candidate = m.group(1).strip()
        candidate = re.sub(
            r"\s*(?:in|for|and|,|\?|during|the year).*$", "", candidate, flags=re.IGNORECASE
        ).strip()
        if not candidate:
            continue
        first_word = candidate.lower().split()[0]
        if first_word in _LOCATION_SKIP_WORDS or re.match(r"^\d+$", candidate):
            continue
        return {"status": "invalid", "mention": candidate}
    return {"status": "none"}


def _extract_columns_from_where(where: str) -> dict:
    result = {}
    if not where:
        return result
    date_match = re.search(
        r'\b([A-Za-z_]\w*)\s*(?:>=|<=|>|<)\s*[\'"](\d{4}-\d{2}-\d{2})', where
    )
    if date_match:
        result["date_column"] = date_match.group(1)
    loc_match = re.search(r'\b([A-Za-z_]\w*)\s*=\s*[\'"](\d+)[\'"]', where)
    if loc_match:
        result["location_column"] = loc_match.group(1)
    return result


def query_socrata(dataset: str, where: str = None, select: str = "count(*) AS total",
                  group: str = None, limit: int = 10) -> dict:
    dataset_id = DATASETS.get(dataset)
    logger.info("[socrata] %s", dataset)
    if not dataset_id:
        return {"error": f"Unknown dataset: {dataset}"}
    params = {"$select": select, "$limit": str(limit)}
    if where:
        params["$where"] = where
    if group:
        params["$group"] = group
    if SOCRATA_APP_TOKEN:
        params["$$app_token"] = SOCRATA_APP_TOKEN
    url = f"{SOCRATA_BASE}/{dataset_id}.json?" + urllib.parse.urlencode(params)
    _sql = f"SELECT {select} FROM {dataset}"
    if where:
        _sql += f" WHERE {where}"
    if group:
        _sql += f" GROUP BY {group}"
    _sql += f" LIMIT {limit}"
    logger.info("[socrata] QUERY: %s", _sql)
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=60, context=_SSL_CTX) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            logger.error("[socrata] HTTP %s for url=%s body=%s", e.code, url, body)
            return {"error": f"HTTP Error {e.code}: {e.reason}", "detail": body}
        except Exception as e:
            if attempt == 0 and "timed out" in str(e).lower():
                logger.warning("[socrata] timeout on attempt 1, retrying: url=%s", url)
                continue
            logger.error("[socrata] error url=%s exc=%s", url, e)
            return {"error": str(e)}


def parse_intent(question: str, history: list = None) -> dict:
    """Use Claude Haiku to extract structured data-query intent from a user question."""
    messages = []
    if history:
        for turn in history[-4:]:
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
    messages.append({"role": "user", "content": question})

    try:
        resp = _claude_create(
            model      = CLAUDE_PRIMARY,
            max_tokens = 256,
            system     = (
                "You extract structured intent from user questions about Chicago city data. "
                "The four available datasets are: crime incidents, building permits, "
                "business licenses, and 311 service requests. "
                "Always call the parse_data_query_intent tool."
            ),
            tools       = [INTENT_TOOL],
            tool_choice = {"type": "tool", "name": "parse_data_query_intent"},
            messages    = messages,
        )
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
    except anthropic.APIStatusError as exc:
        if exc.status_code == 529:
            raise
        logger.warning("[intent] parse failed, defaulting to non-data: %s", exc)
    except Exception as exc:
        logger.warning("[intent] parse failed, defaulting to non-data: %s", exc)

    return {"is_data_query": False, "has_time": False, "has_location": False, "is_citywide": False}


class SocrataSource:
    name = "socrata"

    def tool_definition(self) -> dict:
        return SOCRATA_TOOLS[0] if SOCRATA_TOOLS else {}

    def execute(self, tool_input: dict) -> str:
        import json as _json
        result = query_socrata(
            dataset=tool_input.get("dataset", ""),
            where=tool_input.get("where") or None,
            select=tool_input.get("select") or "count(*) AS total",
            group=tool_input.get("group") or None,
        )
        return _json.dumps(result)
