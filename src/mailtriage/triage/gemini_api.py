"""Google Gemini, for GEMINI_API_KEY. Stdlib only."""

from __future__ import annotations

import json
import os
from typing import Any

from mailtriage.config import Config
from mailtriage.delivery.http import post_json
from mailtriage.errors import MailError

# cfg.model overrides this when non-empty.
MODEL = "gemini-2.5-flash"


def _strip_additional_properties(node: Any) -> Any:
    """Gemini's responseSchema rejects the "additionalProperties" keyword
    anywhere in the tree. TRIAGE_SCHEMA is shared with the other backends'
    plain JSON Schema, so strip it recursively rather than hand-write a
    second schema just for this one."""
    if isinstance(node, dict):
        return {k: _strip_additional_properties(v) for k, v in node.items() if k != "additionalProperties"}
    if isinstance(node, list):
        return [_strip_additional_properties(v) for v in node]
    return node


def call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise MailError(
            "GEMINI_API_KEY is not set. Get a free-tier key at https://aistudio.google.com/apikey, then add "
            "it in your fork: Settings -> Secrets and variables -> Actions -> New repository secret, named "
            "exactly GEMINI_API_KEY."
        )
    model = cfg.model or MODEL
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _strip_additional_properties(schema),
        },
    }
    try:
        status, body = post_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            payload,
            # Never in the URL query -- it ends up in logs.
            {"x-goog-api-key": key},
            timeout=120,
        )
    except Exception as e:
        raise MailError(
            f"could not reach generativelanguage.googleapis.com ({type(e).__name__}: {e}). The runner had "
            "no network or DNS failed. Re-run the workflow by hand."
        ) from e

    if status in (400, 403) and "API_KEY" in body:
        raise MailError(
            "Google rejected GEMINI_API_KEY. The secret is set but the key is wrong, revoked, or has a "
            "stray space. Make a fresh key at https://aistudio.google.com/apikey and update the "
            "GEMINI_API_KEY secret under Settings -> Secrets and variables -> Actions."
        )
    if status == 429:
        raise MailError(
            "Gemini rate-limited this run — the free tier allows roughly 10 requests/minute, or the daily "
            "quota is used up. Nothing to fix in the code; the next scheduled run will pick things up."
        )
    if status >= 300:
        raise MailError(f"Gemini returned HTTP {status}: {body[:500]}")

    try:
        parsed = json.loads(body)
        text = parsed["candidates"][0]["content"]["parts"][0]["text"]
        result: dict[str, Any] = json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise MailError(f"Gemini returned a response mailtriage could not parse: {body[:500]}") from e
    return result
