import os
import time
import logging

import anthropic

from .config import CLAUDE_PRIMARY, CLAUDE_FALLBACK

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""), timeout=30.0)


def _claude_create(*args, _retries: int = 3, _backoff: float = 2.0, **kwargs):
    """Wrapper around client.messages.create with exponential-backoff retry for 529 overload errors.

    Falls back to CLAUDE_FALLBACK after exhausting retries on the primary model.
    """
    primary = kwargs.get("model", CLAUDE_PRIMARY)
    has_tools = bool(kwargs.get("tools"))
    for attempt in range(_retries):
        try:
            t0 = time.monotonic()
            resp = client.messages.create(*args, **kwargs)
            elapsed = time.monotonic() - t0
            usage = resp.usage
            logger.info(
                "[claude] model=%s tools=%s in=%d out=%d stop=%s elapsed=%.2fs",
                resp.model, has_tools,
                usage.input_tokens, usage.output_tokens,
                resp.stop_reason, elapsed,
            )
            return resp
        except anthropic.APIStatusError as exc:
            if exc.status_code == 529 and attempt < _retries - 1:
                wait = _backoff * (2 ** attempt)
                logger.warning(
                    "[claude] Overloaded (529) — retrying in %.1fs (attempt %d/%d)",
                    wait, attempt + 1, _retries,
                )
                time.sleep(wait)
            elif exc.status_code == 529 and primary != CLAUDE_FALLBACK:
                logger.warning(
                    "[claude] %s still overloaded after %d retries — falling back to %s",
                    primary, _retries, CLAUDE_FALLBACK,
                )
                fallback_kwargs = {**kwargs, "model": CLAUDE_FALLBACK}
                t0 = time.monotonic()
                resp = client.messages.create(*args, **fallback_kwargs)
                elapsed = time.monotonic() - t0
                usage = resp.usage
                logger.info(
                    "[claude] model=%s tools=%s in=%d out=%d stop=%s elapsed=%.2fs (fallback)",
                    resp.model, has_tools,
                    usage.input_tokens, usage.output_tokens,
                    resp.stop_reason, elapsed,
                )
                return resp
            else:
                logger.error(
                    "[claude] API error model=%s status=%s: %s",
                    primary, exc.status_code, exc.message,
                )
                raise
