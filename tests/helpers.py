"""Shared by the delivery tests: a Triaged fixture and a stubbed urlopen."""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from typing import Any, cast

from mailtriage.config import Config
from mailtriage.models import Triaged


def item(bucket: str, subject: str = "hi", **overrides: object) -> Triaged:
    base: Triaged = {
        "bucket": bucket,
        "note": "worth a look",
        "account": "work@example.com",
        "sender": "Alice <alice@example.com>",
        "subject": subject,
        "link": "https://mail.example.com/msg/1?a=1&b=2",
        "date": "2026-08-28T00:00:00+00:00",
        "unread": False,
        "idx": 0,
        "draft": "",
    }
    return cast(Triaged, {**base, **overrides})


def cfg(delivery: str, **overrides: object) -> Config:
    c = Config.from_mapping({"delivery": delivery})
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def capture_posts(monkeypatch: Any, status: int = 200, body: str = "ok") -> list[dict[str, Any]]:
    """Replace urlopen under delivery.http; every request lands in the
    returned list as {url, data (bytes), headers (lower-cased names)}."""
    posts: list[dict[str, Any]] = []

    class _Resp:
        def __init__(self) -> None:
            self.status = status

        def read(self) -> bytes:
            return body.encode()

        def __enter__(self) -> _Resp:  # noqa: PYI034
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_urlopen(req: Any, timeout: int | None = None) -> _Resp:
        posts.append({"url": req.full_url, "data": req.data, "headers": {k.lower(): v for k, v in req.header_items()}})
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(body.encode()))  # type: ignore[arg-type]
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return posts
