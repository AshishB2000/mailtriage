"""The one HTTP helper delivery backends share."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

UA = "mailtriage (+https://github.com/AshishB2000/mailtriage)"


def post(url: str, data: bytes, headers: dict[str, str] | None = None, timeout: int = 30) -> tuple[int, str]:
    """POST raw bytes and return (status, body). A 4xx/5xx is returned, not
    raised — every caller needs the response body to explain the failure
    usefully."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str] | None = None, timeout: int = 30
) -> tuple[int, str]:
    return post(
        url, json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json", **(headers or {})}, timeout
    )
