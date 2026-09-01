"""triage.pick is the security layer: every hostile-model case must be handled
here, without a network round trip, no matter which of the five backends
produced the reply. Backend-specific plumbing (claude_api, claude_cli,
codex_cli, openai_api, gemini_api) and provider selection have their own test
files; this one covers the shared pieces: build_system, build_user, pick, and
the triage() -> select_backend -> call -> pick wiring.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mailtriage import triage
from mailtriage.config import Config
from mailtriage.models import Email

CFG = Config(delivery="email", interests="rockets and clocks", reading_count=8)


def make_email(i: int) -> Email:
    return {
        "account": f"acct{i}",
        "from": f"sender{i}@example.com",
        "subject": f"real subject {i}",
        "snippet": f"real snippet {i}",
        "date": "2026-08-28T10:00:00+00:00",
        "unread": True,
        "link": f"https://real.example.com/{i}",
    }


def test_pick_drops_hostile_ids_and_dedupes():
    emails = [make_email(i) for i in range(5)]
    reply = {
        "items": [
            {"id": True, "bucket": "needs_action", "note": "bool id, not an int"},
            {"id": "2", "bucket": "needs_action", "note": "string id"},
            {"id": 99, "bucket": "needs_action", "note": "out of range"},
            {"id": -1, "bucket": "needs_action", "note": "negative"},
            {"id": 0, "bucket": "needs_action", "note": "first, kept"},
            {"id": 0, "bucket": "needs_action", "note": "duplicate of above, dropped"},
            {"id": 1, "bucket": "noise", "note": "unknown bucket, dropped"},
        ]
    }
    picked = triage.pick(CFG, emails, reply)
    assert len(picked) == 1
    assert picked[0]["note"] == "first, kept"


def test_pick_caps_worth_reading_but_not_needs_action():
    emails = [make_email(i) for i in range(20)]
    items = [{"id": i, "bucket": "worth_reading", "note": f"n{i}"} for i in range(12)]
    items += [{"id": i, "bucket": "needs_action", "note": f"a{i}"} for i in range(12, 15)]
    reply = {"items": items}
    picked = triage.pick(CFG, emails, reply)
    worth = [p for p in picked if p["bucket"] == "worth_reading"]
    action = [p for p in picked if p["bucket"] == "needs_action"]
    assert len(worth) == 8  # cfg.reading_count
    assert len(action) == 3  # uncapped


def test_pick_coerces_null_note_to_empty_string():
    """An explicit JSON null (not just a missing key) must not become the string "None"."""
    emails = [make_email(0)]
    reply = {"items": [{"id": 0, "bucket": "needs_action", "note": None}]}
    picked = triage.pick(CFG, emails, reply)
    assert picked[0]["note"] == ""


def test_pick_ignores_model_supplied_fields_uses_real_email():
    emails = [make_email(0)]
    reply = {
        "items": [
            {
                "id": 0,
                "bucket": "needs_action",
                "note": "reply by Friday",
                "link": "https://evil.example.com/phish",
                "subject": "fabricated subject",
            }
        ]
    }
    picked = triage.pick(CFG, emails, reply)
    assert picked[0]["link"] == emails[0]["link"]
    assert picked[0]["subject"] == emails[0]["subject"]
    assert picked[0]["sender"] == emails[0]["from"]
    assert picked[0]["note"] == "reply by Friday"


def test_pick_sorts_needs_action_first_preserving_order():
    emails = [make_email(i) for i in range(4)]
    reply = {
        "items": [
            {"id": 0, "bucket": "worth_reading", "note": "w0"},
            {"id": 1, "bucket": "needs_action", "note": "a1"},
            {"id": 2, "bucket": "worth_reading", "note": "w2"},
            {"id": 3, "bucket": "needs_action", "note": "a3"},
        ]
    }
    picked = triage.pick(CFG, emails, reply)
    assert [p["bucket"] for p in picked] == ["needs_action", "needs_action", "worth_reading", "worth_reading"]
    assert [p["note"] for p in picked] == ["a1", "a3", "w0", "w2"]


def test_build_system_has_interests_and_bucket_names():
    system = triage.build_system(CFG)
    assert "rockets and clocks" in system
    assert "needs_action" in system
    assert "worth_reading" in system


def test_build_user_has_bracketed_index():
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    user = triage.build_user([make_email(0), make_email(1)], now)
    assert "[0]" in user
    assert "[1]" in user


def test_triage_end_to_end_uses_selected_backend(monkeypatch):
    """triage() must call select_backend(), pass it build_system/build_user/
    TRIAGE_SCHEMA, and run the reply through pick() -- wired together, not
    unit by unit."""
    emails = [make_email(0)]
    sentinel: dict[str, Any] = {"items": [{"id": 0, "bucket": "needs_action", "note": "via stub backend"}]}
    seen: dict[str, str] = {}

    def fake_call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        seen["system"] = system
        seen["user"] = user
        assert schema == triage.TRIAGE_SCHEMA
        return sentinel

    def fake_select_backend(cfg: Config, environ: Any) -> tuple[str, Any]:
        return "stub", fake_call

    monkeypatch.setattr(triage, "select_backend", fake_select_backend)
    result = triage.triage(CFG, emails, datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
    assert result[0]["note"] == "via stub backend"
    assert "rockets and clocks" in seen["system"]
    assert "[0]" in seen["user"]
