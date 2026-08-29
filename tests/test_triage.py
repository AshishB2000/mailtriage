"""triage.pick is the security layer: every hostile-model case must be handled
here, without a network round trip. triage.triage is tested against a stub
message object so the tool-use/max_tokens plumbing is covered without a call.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from mailtriage import triage
from mailtriage.config import Config
from mailtriage.errors import MailError
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


class _StubToolUseBlock:
    type = "tool_use"

    def __init__(self, input_: dict[str, Any]):
        self.input = input_


class _StubMessage:
    def __init__(self, stop_reason: str, content: list[Any]):
        self.stop_reason = stop_reason
        self.content = content


def test_triage_happy_path(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    emails = [make_email(0), make_email(1)]
    stub = _StubMessage(
        "end_turn",
        [_StubToolUseBlock({"items": [{"id": 0, "bucket": "needs_action", "note": "reply"}]})],
    )
    monkeypatch.setattr(triage, "_call", lambda cfg, emails, now: stub)
    result = triage.triage(CFG, emails, datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
    assert len(result) == 1
    assert result[0]["bucket"] == "needs_action"
    assert result[0]["note"] == "reply"


def test_triage_raises_on_max_tokens(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    stub = _StubMessage("max_tokens", [])
    monkeypatch.setattr(triage, "_call", lambda cfg, emails, now: stub)
    with pytest.raises(MailError, match="max_tokens"):
        triage.triage(CFG, [make_email(0)], datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))


# --- backend selection -------------------------------------------------


def test_triage_uses_cli_backend_when_oauth_token_set(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sentinel = {"items": [{"id": 0, "bucket": "needs_action", "note": "via cli"}]}
    monkeypatch.setattr(triage, "_call_via_cli", lambda cfg, emails, now: sentinel)
    monkeypatch.setattr(
        triage, "_call", lambda cfg, emails, now: (_ for _ in ()).throw(AssertionError("API path used"))
    )
    result = triage.triage(CFG, [make_email(0)], datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
    assert result[0]["note"] == "via cli"


def test_triage_uses_api_backend_when_only_api_key_set(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    stub = _StubMessage(
        "end_turn", [_StubToolUseBlock({"items": [{"id": 0, "bucket": "needs_action", "note": "via api"}]})]
    )
    monkeypatch.setattr(triage, "_call", lambda cfg, emails, now: stub)
    monkeypatch.setattr(
        triage, "_call_via_cli", lambda cfg, emails, now: (_ for _ in ()).throw(AssertionError("CLI path used"))
    )
    result = triage.triage(CFG, [make_email(0)], datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
    assert result[0]["note"] == "via api"


def test_triage_raises_when_no_auth_configured(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MailError, match="No Claude auth configured"):
        triage.triage(CFG, [make_email(0)], datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))


# --- _call_via_cli parsing ----------------------------------------------


class _StubCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_call_via_cli_parses_structured_output(monkeypatch):
    fake = _StubCompletedProcess(
        0,
        stdout=json.dumps(
            {
                "result": "ok",
                "structured_output": {"items": [{"id": 0, "bucket": "needs_action", "note": "x"}]},
            }
        ),
    )
    monkeypatch.setattr("mailtriage.triage.subprocess.run", lambda *a, **k: fake)
    reply = triage._call_via_cli(CFG, [make_email(0)], datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
    assert reply == {"items": [{"id": 0, "bucket": "needs_action", "note": "x"}]}


def test_call_via_cli_full_triage_run(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    fake = _StubCompletedProcess(
        0,
        stdout=json.dumps(
            {
                "result": "ok",
                "structured_output": {"items": [{"id": 0, "bucket": "needs_action", "note": "reply by Friday"}]},
            }
        ),
    )
    monkeypatch.setattr("mailtriage.triage.subprocess.run", lambda *a, **k: fake)
    emails = [make_email(0)]
    result = triage.triage(CFG, emails, datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
    assert len(result) == 1
    assert result[0]["bucket"] == "needs_action"
    assert result[0]["note"] == "reply by Friday"


def test_call_via_cli_nonzero_exit_raises(monkeypatch):
    fake = _StubCompletedProcess(1, stderr="not authenticated")
    monkeypatch.setattr("mailtriage.triage.subprocess.run", lambda *a, **k: fake)
    with pytest.raises(MailError, match="not authenticated"):
        triage._call_via_cli(CFG, [make_email(0)], datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))


def test_call_via_cli_missing_structured_output_raises(monkeypatch):
    fake = _StubCompletedProcess(0, stdout=json.dumps({"result": "ok"}))
    monkeypatch.setattr("mailtriage.triage.subprocess.run", lambda *a, **k: fake)
    with pytest.raises(MailError, match="structured output"):
        triage._call_via_cli(CFG, [make_email(0)], datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))


def test_call_via_cli_not_installed_raises(monkeypatch):
    def raise_not_found(*a, **k):
        raise FileNotFoundError("no such file: claude")

    monkeypatch.setattr("mailtriage.triage.subprocess.run", raise_not_found)
    with pytest.raises(MailError, match="not installed"):
        triage._call_via_cli(CFG, [make_email(0)], datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))


def test_call_via_cli_surfaces_is_error_from_stdout(monkeypatch):
    # `claude -p` reports failures as an is_error envelope on STDOUT with an
    # empty stderr and a nonzero exit — the real reason must reach the user.
    fake = _StubCompletedProcess(
        1,
        stdout=json.dumps({"is_error": True, "result": "Failed to authenticate: OAuth session expired"}),
        stderr="",
    )
    monkeypatch.setattr("mailtriage.triage.subprocess.run", lambda *a, **k: fake)
    with pytest.raises(MailError, match="Failed to authenticate"):
        triage._call_via_cli(CFG, [make_email(0)], datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))


def test_call_via_cli_result_json_fallback(monkeypatch):
    # Some CLI versions omit structured_output and put the JSON object as a
    # string in `result`.
    fake = _StubCompletedProcess(
        0,
        stdout=json.dumps({"result": json.dumps({"items": [{"id": 0, "bucket": "worth_reading", "note": "fyi"}]})}),
    )
    monkeypatch.setattr("mailtriage.triage.subprocess.run", lambda *a, **k: fake)
    reply = triage._call_via_cli(CFG, [make_email(0)], datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
    assert reply == {"items": [{"id": 0, "bucket": "worth_reading", "note": "fyi"}]}
