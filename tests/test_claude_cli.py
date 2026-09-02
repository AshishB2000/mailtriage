"""claude_cli.call parsing: the `claude -p --output-format json` envelope,
adapted to the call(cfg, system, user, schema) backend contract."""

from __future__ import annotations

import json

import pytest

from mailtriage.config import Config
from mailtriage.errors import MailError
from mailtriage.triage import claude_cli

CFG = Config(delivery="email")
SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}


class _StubCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_call_parses_structured_output(monkeypatch):
    fake = _StubCompletedProcess(
        0,
        stdout=json.dumps(
            {
                "result": "ok",
                "structured_output": {"items": [{"id": 0, "bucket": "needs_action", "note": "x"}]},
            }
        ),
    )
    monkeypatch.setattr("mailtriage.triage.claude_cli.subprocess.run", lambda *a, **k: fake)
    reply = claude_cli.call(CFG, "system prompt", "user prompt", SCHEMA)
    assert reply == {"items": [{"id": 0, "bucket": "needs_action", "note": "x"}]}


def test_call_nonzero_exit_raises(monkeypatch):
    fake = _StubCompletedProcess(1, stderr="not authenticated")
    monkeypatch.setattr("mailtriage.triage.claude_cli.subprocess.run", lambda *a, **k: fake)
    with pytest.raises(MailError, match="not authenticated"):
        claude_cli.call(CFG, "system prompt", "user prompt", SCHEMA)


def test_call_missing_structured_output_raises(monkeypatch):
    fake = _StubCompletedProcess(0, stdout=json.dumps({"result": "ok"}))
    monkeypatch.setattr("mailtriage.triage.claude_cli.subprocess.run", lambda *a, **k: fake)
    with pytest.raises(MailError, match="structured output"):
        claude_cli.call(CFG, "system prompt", "user prompt", SCHEMA)


def test_call_not_installed_raises(monkeypatch):
    def raise_not_found(*a, **k):
        raise FileNotFoundError("no such file: claude")

    monkeypatch.setattr("mailtriage.triage.claude_cli.subprocess.run", raise_not_found)
    with pytest.raises(MailError, match="not installed"):
        claude_cli.call(CFG, "system prompt", "user prompt", SCHEMA)


def test_call_surfaces_is_error_from_stdout(monkeypatch):
    # `claude -p` reports failures as an is_error envelope on STDOUT with an
    # empty stderr and a nonzero exit — the real reason must reach the user.
    fake = _StubCompletedProcess(
        1,
        stdout=json.dumps({"is_error": True, "result": "Failed to authenticate: OAuth session expired"}),
        stderr="",
    )
    monkeypatch.setattr("mailtriage.triage.claude_cli.subprocess.run", lambda *a, **k: fake)
    with pytest.raises(MailError, match="Failed to authenticate"):
        claude_cli.call(CFG, "system prompt", "user prompt", SCHEMA)


def test_call_result_json_fallback(monkeypatch):
    # Some CLI versions omit structured_output and put the JSON object as a
    # string in `result`.
    fake = _StubCompletedProcess(
        0,
        stdout=json.dumps({"result": json.dumps({"items": [{"id": 0, "bucket": "worth_reading", "note": "fyi"}]})}),
    )
    monkeypatch.setattr("mailtriage.triage.claude_cli.subprocess.run", lambda *a, **k: fake)
    reply = claude_cli.call(CFG, "system prompt", "user prompt", SCHEMA)
    assert reply == {"items": [{"id": 0, "bucket": "worth_reading", "note": "fyi"}]}


def test_call_honors_model_override(monkeypatch):
    fake = _StubCompletedProcess(0, stdout=json.dumps({"structured_output": {"items": []}}))
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return fake

    monkeypatch.setattr("mailtriage.triage.claude_cli.subprocess.run", fake_run)
    cfg = Config(delivery="email", model="claude-custom-9000")
    claude_cli.call(cfg, "system prompt", "user prompt", SCHEMA)
    assert "claude-custom-9000" in seen["args"]


def test_call_uses_system_prompt_flag_and_no_model_by_default(monkeypatch, capsys):
    """v1's working invocation never pinned a model; --system-prompt keeps the
    model a triager rather than Claude Code's coding persona."""
    fake = _StubCompletedProcess(
        0,
        stdout=json.dumps(
            {
                "result": "SECRET RESULT TEXT",
                "structured_output": {"items": []},
                "modelUsage": {"claude-x": {}},
                "num_turns": 1,
                "stop_reason": "end_turn",
            }
        ),
    )
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return fake

    monkeypatch.setattr("mailtriage.triage.claude_cli.subprocess.run", fake_run)
    claude_cli.call(CFG, "SYSTEM TEXT", "USER TEXT", SCHEMA)
    args = seen["args"]
    assert "--model" not in args
    assert args[args.index("--system-prompt") + 1] == "SYSTEM TEXT"
    assert args[args.index("-p") + 1] == "USER TEXT"
    assert "--no-session-persistence" in args

    err = capsys.readouterr().err
    assert "claude CLI envelope" in err and "'models': ['claude-x']" in err and "'structured': True" in err
    assert "SECRET RESULT TEXT" not in err
