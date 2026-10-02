"""Thin wrapper over an OpenAI-compatible chat completions endpoint, shared
by every agent. Provider is chosen by LLM_PROVIDER ("openrouter" or
"gemini"); both sets of credentials can sit in .env at once so switching
back is a one-line change."""

import json
import logging
import os
import re
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

log = logging.getLogger("gitshow.llm")

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openrouter").strip().lower()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# "-latest" aliases always resolve to Google's current stable model for that
# tier, so this keeps tracking their cheapest Flash tier without needing a
# code change whenever a new point release ships.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen3.7-flash")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

if LLM_PROVIDER == "gemini":
    API_KEY, MODEL, BASE_URL = GEMINI_API_KEY, GEMINI_MODEL, GEMINI_BASE_URL
else:
    LLM_PROVIDER = "openrouter"
    API_KEY, MODEL, BASE_URL = OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_BASE_URL

REQUEST_TIMEOUT_SECONDS = 90
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_PLACEHOLDERS = {"your_gemini_api_key_here", "your_openrouter_api_key_here"}

_client = OpenAI(api_key=API_KEY, base_url=BASE_URL) if API_KEY else None


class LLMError(Exception):
    pass


def is_configured() -> bool:
    return bool(API_KEY) and API_KEY not in _PLACEHOLDERS


def complete(messages, tools=None, json_mode=False, schema=None):
    """One chat completion. Retries once on rate limits and server errors;
    raises LLMError otherwise. schema (a JSON Schema) constrains the reply
    to that exact shape via structured output."""
    if _client is None:
        raise LLMError(
            f"{'OPENROUTER_API_KEY' if LLM_PROVIDER == 'openrouter' else 'GEMINI_API_KEY'} "
            "is not configured. Set it in backend/.env."
        )
    kwargs = {"model": MODEL, "messages": messages, "timeout": REQUEST_TIMEOUT_SECONDS}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if schema:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema.get("title", "reply"), "schema": schema, "strict": True},
        }
    elif json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    for attempt in range(2):
        try:
            return _client.chat.completions.create(**kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if attempt == 0 and (status in RETRYABLE_STATUS or status is None):
                time.sleep(2)
                continue
            raise LLMError(f"{LLM_PROVIDER} request failed: {exc}") from exc


JSON_REPAIR_ATTEMPTS = 2


def complete_json(messages, schema=None, validate=None) -> dict:
    """A completion that must return a JSON object (matching schema, if
    given), with repair attempts if the reply doesn't parse or validate()
    raises ValueError on it."""
    text = complete(messages, json_mode=True, schema=schema).choices[0].message.content or ""
    for attempt in range(JSON_REPAIR_ATTEMPTS + 1):
        try:
            value = parse_json(text)
            if validate:
                validate(value)
            return value
        except ValueError as exc:
            if attempt == JSON_REPAIR_ATTEMPTS:
                log.warning("Unparseable JSON from model: %r", text[:2000])
                raise
            repair = messages + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": f"That reply couldn't be used ({exc}). Reply again with only one JSON object, nothing else.",
                },
            ]
            text = complete(repair, json_mode=True, schema=schema).choices[0].message.content or ""


def parse_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise ValueError("no JSON object in the reply") from None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
    # Some models wrap the object in a one-element array.
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        value = value[0]
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object, got {type(value).__name__}")
    return value


EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0000FE0F"
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text: str) -> str:
    return EMOJI_PATTERN.sub("", text or "")
