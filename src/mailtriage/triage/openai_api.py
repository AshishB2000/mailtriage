"""OpenAI Chat Completions, for OPENAI_API_KEY. Stdlib only."""

from __future__ import annotations

import json
import os
from typing import Any

from mailtriage.config import Config
from mailtriage.delivery.http import post_json
from mailtriage.errors import MailError

# cfg.model overrides this when non-empty.
MODEL = "gpt-5.6-luna"


def call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise MailError(
            "OPENAI_API_KEY is not set. Get a key at https://platform.openai.com/api-keys, then add it in "
            "your fork: Settings -> Secrets and variables -> Actions -> New repository secret, named "
            "exactly OPENAI_API_KEY."
        )
    payload = {
        "model": cfg.model or MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "emit", "strict": True, "schema": schema},
        },
    }
    try:
        status, body = post_json(
            "https://api.openai.com/v1/chat/completions",
            payload,
            {"Authorization": f"Bearer {key}"},
            timeout=120,
        )
    except Exception as e:
        raise MailError(
            f"could not reach api.openai.com ({type(e).__name__}: {e}). The runner had no network or DNS "
            "failed. Re-run the workflow by hand."
        ) from e

    if status == 401:
        raise MailError(
            "OpenAI rejected OPENAI_API_KEY. The secret is set but the key is wrong, revoked, or has a "
            "stray space. Make a fresh key at https://platform.openai.com/api-keys and update the "
            "OPENAI_API_KEY secret under Settings -> Secrets and variables -> Actions."
        )
    if status == 429:
        if "insufficient_quota" in body:
            raise MailError(
                "OpenAI says this account is out of credit (insufficient_quota in the response). Nothing to "
                "fix in the code — check billing at https://platform.openai.com/settings/organization/billing; "
                "the next scheduled run will pick things up."
            )
        raise MailError(
            "OpenAI rate-limited this run. Nothing to fix in the code — the next scheduled run will pick things up."
        )
    if status >= 300:
        raise MailError(f"OpenAI returned HTTP {status}: {body[:500]}")

    try:
        content = json.loads(body)["choices"][0]["message"]["content"]
        result: dict[str, Any] = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise MailError(f"OpenAI returned a response mailtriage could not parse: {body[:500]}") from e
    return result
