"""claude_api.call adds the API-shape checks (max_tokens, no tool_use block)
on top of the anthropic SDK call. Stubs anthropic.Anthropic itself rather than
call() -- these checks run on the real SDK response shape, not before it."""

from __future__ import annotations

from typing import Any

import anthropic
import pytest

from mailtriage.config import Config
from mailtriage.errors import MailError
from mailtriage.triage import claude_api

CFG = Config(delivery="email", interests="rockets and clocks", reading_count=8)
SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}


class _StubToolUseBlock:
    type = "tool_use"

    def __init__(self, input_: dict[str, Any]):
        self.input = input_


class _StubMessage:
    def __init__(self, stop_reason: str, content: list[Any]):
        self.stop_reason = stop_reason
        self.content = content


class _StubMessages:
    def __init__(self, resp: _StubMessage):
        self._resp = resp

    def create(self, **kwargs: Any) -> _StubMessage:
        return self._resp


class _StubClient:
    def __init__(self, resp: _StubMessage):
        self.messages = _StubMessages(resp)


def test_call_happy_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    stub = _StubMessage("end_turn", [_StubToolUseBlock({"items": [{"id": 0, "bucket": "needs_action"}]})])
    monkeypatch.setattr(anthropic, "Anthropic", lambda: _StubClient(stub))
    result = claude_api.call(CFG, "system prompt", "user prompt", SCHEMA)
    assert result == {"items": [{"id": 0, "bucket": "needs_action"}]}


def test_call_raises_on_max_tokens(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    stub = _StubMessage("max_tokens", [])
    monkeypatch.setattr(anthropic, "Anthropic", lambda: _StubClient(stub))
    with pytest.raises(MailError, match="max_tokens"):
        claude_api.call(CFG, "system prompt", "user prompt", SCHEMA)


def test_call_raises_when_no_tool_use_block(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    stub = _StubMessage("end_turn", [])
    monkeypatch.setattr(anthropic, "Anthropic", lambda: _StubClient(stub))
    with pytest.raises(MailError, match="no triage at all"):
        claude_api.call(CFG, "system prompt", "user prompt", SCHEMA)


def test_call_raises_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MailError, match="ANTHROPIC_API_KEY"):
        claude_api.call(CFG, "system prompt", "user prompt", SCHEMA)


def test_call_honors_model_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    stub = _StubMessage("end_turn", [_StubToolUseBlock({"items": []})])
    seen: dict[str, Any] = {}

    class _RecordingMessages(_StubMessages):
        def create(self, **kwargs: Any) -> _StubMessage:
            seen.update(kwargs)
            return self._resp

    class _RecordingClient(_StubClient):
        def __init__(self, resp: _StubMessage):
            self.messages = _RecordingMessages(resp)

    monkeypatch.setattr(anthropic, "Anthropic", lambda: _RecordingClient(stub))
    cfg = Config(delivery="email", model="claude-custom-9000")
    claude_api.call(cfg, "system prompt", "user prompt", SCHEMA)
    assert seen["model"] == "claude-custom-9000"
