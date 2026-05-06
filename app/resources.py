import gc
import os
import logging

import psycopg2.pool
import voyageai

from .config import DATABASE_URL
from . import db as _db_module
from . import data_sources as ds
from .data_sources import CONTEXT_SOURCES, TOOL_SOURCES
from .data_sources import elms as _elms
from .data_sources import rag as _rag
from .data_sources import socrata as _socrata
from .data_sources import illinois_socrata as _il_socrata
from .data_sources import legiscan as _legiscan

logger = logging.getLogger(__name__)


def load_resources() -> None:
    """Initialise all shared resources once per gunicorn worker."""

    # 1. Community areas + Socrata dataset schemas (city + state)
    _socrata.load_community_areas()
    _socrata.load_dataset_schemas()
    _socrata.SOCRATA_TOOLS = _socrata.build_socrata_tools()
    _il_socrata.load_illinois_dataset_schemas()
    _il_socrata.ILLINOIS_SOCRATA_TOOLS = _il_socrata.build_illinois_socrata_tools()

    # 2. Voyage AI client
    print("Initialising Voyage AI client...", flush=True)
    _rag._voyage = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"], timeout=30)

    # 3. PostgreSQL connection pool
    print("Connecting to Supabase...", flush=True)
    conn_params = _db_module._ipv4_connect_params(DATABASE_URL)
    _db_module._pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2, maxconn=10, connect_timeout=10, **conn_params
    )

    # 4. Load scrape metadata
    from .db import _db
    with _db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT scrape_date, total_chunks FROM scrape_info ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("No scrape data found. Run `python scrape_and_index.py` first.")
        _rag.SCRAPE_DATE, _rag.TOTAL_CHUNKS = row

    # 5. Pre-populate plain language title caches (city + state)
    _elms.preload_plain_language_cache()
    _legiscan.preload_il_plain_language_cache()

    # 6. Register data source instances
    rag_source = _rag.RAGSource()
    socrata_source = _socrata.SocrataSource()

    CONTEXT_SOURCES.clear()
    CONTEXT_SOURCES.append(rag_source)

    il_socrata_source = _il_socrata.IllinoisSocrataSource()

    TOOL_SOURCES.clear()
    TOOL_SOURCES.append(socrata_source)
    TOOL_SOURCES.append(il_socrata_source)

    gc.collect()
    print(f"Ready — {_rag.TOTAL_CHUNKS} chunks in Supabase, scraped {_rag.SCRAPE_DATE}\n", flush=True)
