import json
import logging
import ssl
import urllib.parse
import urllib.request
from pathlib import Path

import certifi
import os

from ..config import ILLINOIS_SOCRATA_BASE

logger = logging.getLogger(__name__)

ILLINOIS_DATASETS = {
    "il_unemployment_claims": "7set-k26h",
    "il_traffic_crashes":     "8mzk-wtze",
    "il_medicaid_enrollment": "ytpe-fmkj",
    "il_school_report_card":  "fmkp-yad6",
    "il_food_inspections":    "t34d-dfxb",
    "il_public_health_stats": "dm5n-v6ku",
}

ILLINOIS_DATASET_LABELS: dict[str, str] = {
    "il_unemployment_claims": "Illinois unemployment insurance claims (IDES)",
    "il_traffic_crashes":     "Illinois statewide traffic crashes (IDOT)",
    "il_medicaid_enrollment": "Illinois Medicaid/CHIP enrollment (IDHS)",
    "il_school_report_card":  "Illinois school report card (ISBE)",
    "il_food_inspections":    "Illinois food establishment inspections (IDPH)",
    "il_public_health_stats": "Illinois public health statistics (IDPH)",
}

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

_HERE = Path(__file__).parent.parent.parent  # project root
ILLINOIS_SCHEMA_CACHE: dict[str, list] = {}
ILLINOIS_SCHEMA_CACHE_PATH = _HERE / "il_dataset_schemas.json"

ILLINOIS_SOCRATA_TOOLS: list = []


def load_illinois_dataset_schemas() -> None:
    global ILLINOIS_SCHEMA_CACHE
    if ILLINOIS_SCHEMA_CACHE_PATH.exists():
        with open(ILLINOIS_SCHEMA_CACHE_PATH) as f:
            ILLINOIS_SCHEMA_CACHE = json.load(f)
        print(f"Loaded IL dataset schemas from cache ({len(ILLINOIS_SCHEMA_CACHE)} datasets).", flush=True)
        return
    schemas: dict[str, list] = {}
    for key, dataset_id in ILLINOIS_DATASETS.items():
        try:
            print(f"Fetching IL dataset schema for {key}...", flush=True)
            url = f"https://data.illinois.gov/api/views/{dataset_id}.json"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
                data = json.loads(resp.read().decode())
            schemas[key] = [
                {"fieldName": col["fieldName"], "dataTypeName": col["dataTypeName"]}
                for col in data.get("columns", [])
                if "hidden" not in col.get("flags", [])
            ]
            print(f"  {key}: {len(schemas[key])} columns", flush=True)
        except Exception as e:
            print(f"  WARNING: could not fetch IL schema for {key}: {e}", flush=True)
            schemas[key] = []
    ILLINOIS_SCHEMA_CACHE = schemas
    with open(ILLINOIS_SCHEMA_CACHE_PATH, "w") as f:
        json.dump(schemas, f, indent=2)
    print(f"IL dataset schemas saved to {ILLINOIS_SCHEMA_CACHE_PATH.name}", flush=True)


def build_illinois_socrata_tools() -> list:
    dataset_descriptions = "\n".join(
        f"  - {key}: {label}" for key, label in ILLINOIS_DATASET_LABELS.items()
    )
    return [
        {
            "name": "query_illinois_data",
            "description": (
                "Query the Illinois Open Data Portal (data.illinois.gov) for live state-level statistics. "
                "ONLY call this tool for questions about Illinois state government data. "
                "Available datasets:\n" + dataset_descriptions + "\n"
                "Do NOT call for City of Chicago datasets — use query_chicago_data for those."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "dataset": {
                        "type": "string",
                        "enum": list(ILLINOIS_DATASETS.keys()),
                        "description": "Which Illinois state dataset to query",
                    },
                    "where": {
                        "type": "string",
                        "description": (
                            "SODA $where clause using SoQL syntax. Examples:\n"
                            "  date >= '2024-01-01' AND date < '2025-01-01'\n"
                            "  county = 'Cook'\n"
                            "MONTH-END RULE: Always use the first day of the NEXT month as the exclusive "
                            "upper bound for a month range. Never use the last day of the month."
                        ),
                    },
                    "select": {
                        "type": "string",
                        "description": (
                            "SODA $select clause.\n"
                            "HOW MANY questions: use \"count(*) AS total\".\n"
                            "BREAKDOWN questions: use \"<group_col>, count(*) AS total\".\n"
                            "Default to \"count(*) AS total\" when the intent is ambiguous."
                        ),
                    },
                    "group": {
                        "type": "string",
                        "description": (
                            "Column name to group by, e.g. \"county\" or \"year\". "
                            "Do NOT include the words 'GROUP BY' — just the column name."
                        ),
                    },
                },
                "required": ["dataset"],
            },
        }
    ]


def query_illinois_socrata(
    dataset: str,
    where: str = None,
    select: str = "count(*) AS total",
    group: str = None,
    limit: int = 10,
) -> dict:
    dataset_id = ILLINOIS_DATASETS.get(dataset)
    logger.info("[il_socrata] %s", dataset)
    if not dataset_id:
        return {"error": f"Unknown Illinois dataset: {dataset}"}
    params = {"$select": select, "$limit": str(limit)}
    if where:
        params["$where"] = where
    if group:
        params["$group"] = group
    url = f"{ILLINOIS_SOCRATA_BASE}/{dataset_id}.json?" + urllib.parse.urlencode(params)
    _sql = f"SELECT {select} FROM {dataset}"
    if where:
        _sql += f" WHERE {where}"
    if group:
        _sql += f" GROUP BY {group}"
    _sql += f" LIMIT {limit}"
    logger.info("[il_socrata] QUERY: %s", _sql)
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
            logger.error("[il_socrata] HTTP %s for url=%s body=%s", e.code, url, body)
            return {"error": f"HTTP Error {e.code}: {e.reason}", "detail": body}
        except Exception as e:
            if attempt == 0 and "timed out" in str(e).lower():
                logger.warning("[il_socrata] timeout on attempt 1, retrying: url=%s", url)
                continue
            logger.error("[il_socrata] error url=%s exc=%s", url, e)
            return {"error": str(e)}


class IllinoisSocrataSource:
    name = "illinois_socrata"

    def tool_definition(self) -> dict:
        return ILLINOIS_SOCRATA_TOOLS[0] if ILLINOIS_SOCRATA_TOOLS else {}

    def execute(self, tool_input: dict) -> str:
        result = query_illinois_socrata(
            dataset=tool_input.get("dataset", ""),
            where=tool_input.get("where") or None,
            select=tool_input.get("select") or "count(*) AS total",
            group=tool_input.get("group") or None,
        )
        return json.dumps(result)
