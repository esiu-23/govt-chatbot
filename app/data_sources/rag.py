import logging

import numpy as np
import psycopg2.extras

from ..config import MODEL_NAME, TOP_K, SCORE_THRESHOLD
from ..db import _db

logger = logging.getLogger(__name__)

# Set by resources.load_resources()
_voyage = None
SCRAPE_DATE: str = ""
TOTAL_CHUNKS: int = 0


class RAGSource:
    name = "rag"

    def fetch(self, question: str, embedding: list) -> str:
        """Search pgvector for top-K chunks above threshold; return formatted context string."""
        q_vec = np.array(embedding, dtype=np.float32)
        with _db() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                WITH ranked AS (
                    SELECT id, url, title, text,
                           embedding <=> %s AS distance
                    FROM chunks
                    ORDER BY distance
                    LIMIT %s
                )
                SELECT id, url, title, text, 1 - distance AS similarity
                FROM ranked
                """,
                (q_vec, TOP_K),
            )
            rows = cur.fetchall()

        context_parts = []
        sources = []
        seen_urls: set = set()
        for row in rows:
            if row["similarity"] < SCORE_THRESHOLD:
                continue
            context_parts.append(f"[Source: {row['title']}]\n{row['text']}")
            if row["url"] not in seen_urls:
                seen_urls.add(row["url"])
                sources.append({"title": row["title"], "url": row["url"]})

        self._last_sources = sources
        return "\n\n---\n\n".join(context_parts)

    def embed(self, question: str) -> list:
        result = _voyage.embed([question], model=MODEL_NAME, input_type="query")
        return result.embeddings[0]
