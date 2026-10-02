"""Client for Wity (https://wity.alphanimble.com/docs): a decision API that
answers bounded questions (pick an option, yes/no probability) instead of
generating text. Used where Git Show only needs a decision; WITY_API_KEY in
.env turns it on, and callers fall back to the main model without it."""

import logging
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("gitshow.wity")

WITY_API_KEY = os.getenv("WITY_API_KEY")
WITY_BASE_URL = os.getenv("WITY_BASE_URL", "https://wity-proxy-production-2c33.up.railway.app").rstrip("/")

REQUEST_TIMEOUT_SECONDS = 15
RETRYABLE_STATUS = {502, 503}
MAX_STATE_CHARS = 32000  # Wity's limit on the state, after JSON encoding.

_PLACEHOLDERS = {"your_wity_api_key_here"}


class WityError(Exception):
    pass


def is_configured() -> bool:
    return bool(WITY_API_KEY) and WITY_API_KEY not in _PLACEHOLDERS


def decide(state: str, questions: dict) -> dict:
    """Ask Wity the named questions about the state; returns the `answers`
    object."""
    return _post("/v1/systemone", {"state": state, "questions": questions, "reasoning": "auto"})["answers"]


def generate(state: str, instructions: str, shape: dict) -> dict:
    """Short structured text from Wity (it writes at most 100 tokens): the
    parsed object for `shape`, a JSON Schema."""
    value = _post("/v1/generate", {"state": state, "instructions": instructions, "shape": shape})["value"]
    if not isinstance(value, dict):
        raise WityError("wity's generated reply was cut off or not an object")
    return value


def _post(path: str, body: dict) -> dict:
    """POST to Wity. Retries once on 502/503; raises WityError otherwise."""
    if not is_configured():
        raise WityError("WITY_API_KEY is not configured. Set it in backend/.env.")
    # Keep the end of the state: the latest messages matter most.
    if len(body["state"]) > MAX_STATE_CHARS - 1000:
        body = {**body, "state": body["state"][-(MAX_STATE_CHARS - 1000):]}
    headers = {"Authorization": f"Bearer {WITY_API_KEY}"}
    for attempt in range(2):
        try:
            response = requests.post(WITY_BASE_URL + path, json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(1)
                continue
            raise WityError(f"wity request failed: {exc}") from exc
        if response.status_code in RETRYABLE_STATUS and attempt == 0:
            time.sleep(1)
            continue
        if response.status_code != 200:
            raise WityError(f"wity returned {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise WityError(f"wity sent an unreadable reply: {exc}") from exc
    raise WityError("wity request failed")


def yes(answer: dict, threshold: float = 0.5) -> bool:
    """A noul answer as a bool: its probability of yes against a threshold."""
    return float(answer["noul"]) >= threshold
