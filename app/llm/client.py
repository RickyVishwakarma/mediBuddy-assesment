"""Thin wrapper over the Gemini SDK.

Two functions -- structured() and text() -- are the entire surface the rest of the app
touches, so swapping provider means rewriting this file and nothing else. The SDK is
used directly rather than through a chat-model abstraction because there are exactly
three call sites and each one wants a tight schema, not a conversation object.
"""

from __future__ import annotations

import logging
import random
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable, TypeVar

from pydantic import BaseModel

from app.config import (
    GEMINI_MODEL_COMPOSE,
    GEMINI_MODEL_FAST,
    GOOGLE_API_KEY,
    LLM_MAX_RETRIES,
    PROMPT_DIR,
)

T = TypeVar("T", bound=BaseModel)

log = logging.getLogger(__name__)

# The SDK warns about automatic function calling whenever a response_schema is passed.
# We never register tools, so the warning is noise on every structured call.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

# Free-tier Gemini allows only a handful of requests per minute, and one question can
# use three calls. Without backoff a burst turns into a user-visible failure that isn't
# really a failure -- just impatience.
_RETRYABLE = (
    # quota and server-side transients
    "429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL",
    # transport transients -- a dropped connection or a DNS hiccup is not a real answer
    "Server disconnected", "getaddrinfo", "Connection", "ConnectError",
    "ReadTimeout", "ConnectTimeout", "RemoteProtocolError", "Temporary failure",
)
_RETRY_DELAY_RE = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)


def _is_retryable(error: Exception) -> bool:
    message = str(error)
    # A per-DAY quota is not worth waiting out: the server still sends a ~60s retry hint
    # for it, but the budget will not come back this run. Fail fast and honestly instead
    # of sleeping through several minutes for a result that cannot arrive.
    if "PerDay" in message or "per day" in message.lower():
        return False
    return any(marker in message for marker in _RETRYABLE)


def _retry_delay(error: Exception, attempt: int) -> float:
    """Honour the server's own retry hint when it gives one, else exponential backoff."""
    match = _RETRY_DELAY_RE.search(str(error))
    if match:
        return min(float(match.group(1)) + 1.0, 65.0)
    return min(2.0**attempt + random.uniform(0, 1), 65.0)


def _with_retries(call: Callable[[], object]) -> object:
    last: Exception | None = None
    for attempt in range(LLM_MAX_RETRIES + 1):
        try:
            return call()
        except Exception as exc:
            last = exc
            if attempt >= LLM_MAX_RETRIES or not _is_retryable(exc):
                break
            delay = _retry_delay(exc, attempt)
            log.warning("model call rate-limited; retrying in %.1fs", delay)
            time.sleep(delay)
    raise LLMUnavailable(f"model call failed: {last}")


class LLMUnavailable(Exception):
    """The model could not be reached or returned something unusable.

    Callers must degrade honestly rather than substituting their own answer.
    """


@lru_cache(maxsize=1)
def _client():
    if not GOOGLE_API_KEY:
        raise LLMUnavailable(
            "GOOGLE_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover
        raise LLMUnavailable("google-genai is not installed") from exc
    return genai.Client(api_key=GOOGLE_API_KEY)


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    """Prompts live in app/llm/prompts/*.md so wording can be tuned without editing code."""
    path = Path(PROMPT_DIR) / f"{name}.md"
    if not path.is_file():
        raise LLMUnavailable(f"prompt file missing: {path}")
    return path.read_text(encoding="utf-8")


def structured(system: str, user: str, schema: type[T], temperature: float = 0.0) -> T:
    """Call the model and parse into `schema`. Used for intent and the semantic judge."""
    from google.genai import types

    def call():
        return _client().models.generate_content(
            model=GEMINI_MODEL_FAST,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=temperature,
            ),
        )

    response = _with_retries(call)

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, schema):
        return parsed

    # Fall back to parsing the raw text ourselves before giving up.
    raw = getattr(response, "text", None)
    if raw:
        try:
            return schema.model_validate_json(raw)
        except Exception as exc:
            raise LLMUnavailable(f"model returned unparseable JSON: {exc}") from exc
    raise LLMUnavailable("model returned an empty response")


def text(system: str, user: str, temperature: float = 0.2) -> str:
    """Call the model for prose. Used only by compose_answer, whose output is then
    checked by the grounding guard before it can reach a user."""
    from google.genai import types

    def call():
        return _client().models.generate_content(
            model=GEMINI_MODEL_COMPOSE,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=temperature,
            ),
        )

    response = _with_retries(call)
    out = (getattr(response, "text", None) or "").strip()
    if not out:
        raise LLMUnavailable("model returned an empty response")
    return out
