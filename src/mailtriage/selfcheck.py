"""The offline pre-flight check that runs before any API spend.

This is deliberately *not* pytest: it must run on a fork's Actions runner
where `anthropic` may not even be importable yet, catch a logic regression
before the model is ever called, and need no test framework to do it.
`tests/` covers the same functions in more depth; this is the fast gate that
ships with the workflow.

HARD CONSTRAINT: this module must import ONLY pure functions. `triage.pick`
is safe because `triage.py` imports `anthropic` lazily inside `_call`, not at
module scope — importing `pick` here must never drag `anthropic` in.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mailtriage.config import Config
from mailtriage.imap_pull import parse_message, within_window
from mailtriage.triage import pick


def _email(i: int) -> dict:
    return {
        "account": "acct",
        "from": f"sender{i}@example.com",
        "subject": f"subject-{i}",
        "snippet": f"snippet-{i}",
        "date": "2026-08-28T10:00:00+00:00",
        "unread": False,
        "link": f"https://real.example.com/{i}",
    }


def self_check() -> None:
    # 1. Parser + date landmine: parsedate_to_datetime must stay tz-aware, or
    # within_window's UTC arithmetic silently shifts the window by hours.
    raw = (
        b"From: sender@example.com\r\n"
        b"To: me@gmail.com\r\n"
        b"Subject: hi\r\n"
        b"Date: Fri, 28 Aug 2026 09:00:00 +0000\r\n"
        b"Message-ID: <abc123@example.com>\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"hello world\r\n"
    )
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    rec = parse_message(raw, "me@gmail.com", "1 (FLAGS () BODY[]", now, 13)
    assert rec is not None, "an in-window message with a valid Date header must not be dropped"
    assert rec["subject"] == "hi", "parse_message must copy Subject through unchanged"
    assert rec["date"] == "2026-08-28T09:00:00+00:00", (
        "date landmine — parsedate_to_datetime must stay tz-aware, or the digest window silently shifts by hours"
    )

    # 2. Window drops: undated must be excluded, future must clamp, small skew allowed.
    assert within_window(None, now, 13) is False, "undated must drop, or an undated feed floods every run"
    assert within_window(now + timedelta(hours=1), now, 13) is False, (
        "future must clamp, or a sender with a fast clock stays permanently 'recent'"
    )
    assert within_window(now + timedelta(seconds=60), now, 13) is True, (
        "5-minute skew must be allowed, or borderline messages vanish at the window edge"
    )

    # 3. pick() is the security layer: every hostile-model case must be handled
    # here, without a network round trip.
    emails = [_email(i) for i in range(14)]
    cfg = Config(delivery="email", reading_count=8)
    items = [
        {"id": 0, "bucket": "needs_action", "note": "reply to boss"},
        {"id": 1, "bucket": "needs_action", "note": "pay invoice"},
        # hostile: model tries to overwrite the real link/subject for id 2.
        {"id": 2, "bucket": "needs_action", "note": "rsvp", "link": "http://evil.example/", "subject": "EVIL"},
        {"id": 13, "bucket": "noise", "note": "unknown bucket, must be dropped"},
        {"id": 99, "bucket": "worth_reading", "note": "out of range, must be dropped"},
        {"id": True, "bucket": "worth_reading", "note": "bool id, must be dropped"},
        *({"id": i, "bucket": "worth_reading", "note": f"note-{i}"} for i in range(3, 13)),  # 10 candidates
        {"id": 3, "bucket": "worth_reading", "note": "duplicate of above, must be dropped"},
    ]
    picked = pick(cfg, emails, {"items": items})
    needs_action = [p for p in picked if p["bucket"] == "needs_action"]
    worth_reading = [p for p in picked if p["bucket"] == "worth_reading"]

    assert not any("must be dropped" in p["note"] for p in picked), (
        "an unknown bucket, an out-of-range id, or a bool id was accepted instead of dropped"
    )
    assert len(needs_action) == 3, "needs_action must keep every item — it has no cap"
    assert len(worth_reading) == 8, (
        f"worth_reading must be capped at cfg.reading_count ({cfg.reading_count}), or padding kills the digest"
    )
    assert len({p["subject"] for p in worth_reading}) == 8, (
        "a duplicate id in the model reply must be deduped, not counted twice"
    )
    injected = next(p for p in needs_action if p["note"] == "rsvp")
    assert injected["link"] == emails[2]["link"] and injected["subject"] == emails[2]["subject"], (
        "link/subject must always come from the real Email, never from model-supplied fields in the reply"
    )

    print("self-check: ok")
