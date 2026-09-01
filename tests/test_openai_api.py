"""openai_api.call, transport monkeypatched at the module's post_json seam."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mailtriage.config import Config
from mailtriage.errors import MailError
from mailtriage.triage import openai_api

CFG = Config(delivery="email")
SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}


def _reply(items: list[dict[str, Any]]) -> str:
    return json.dumps({"choices": [{"message": {"content": json.dumps({"items": items})}}]})


def test_call_missing_key_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MailError, match="OPENAI_API_KEY"):
        openai_api.call(CFG, "system", "user", SCHEMA)


def test_call_happy_path(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(openai_api, "post_json", lambda *a, **k: (200, _reply([{"id": 0, "bucket": "noise"}])))
    result = openai_api.call(CFG, "system", "user", SCHEMA)
    assert result == {"items": [{"id": 0, "bucket": "noise"}]}


def test_call_401_raises_mentioning_secret(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-bad")
    monkeypatch.setattr(openai_api, "post_json", lambda *a, **k: (401, "{}"))
    with pytest.raises(MailError, match="OPENAI_API_KEY"):
        openai_api.call(CFG, "system", "user", SCHEMA)


def test_call_429_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(openai_api, "post_json", lambda *a, **k: (429, '{"error":{"code":"rate_limit"}}'))
    with pytest.raises(MailError, match="rate-limited"):
        openai_api.call(CFG, "system", "user", SCHEMA)


def test_call_429_quota_message_mentions_billing(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(openai_api, "post_json", lambda *a, **k: (429, '{"error":{"code":"insufficient_quota"}}'))
    with pytest.raises(MailError, match="insufficient_quota"):
        openai_api.call(CFG, "system", "user", SCHEMA)


def test_call_malformed_body_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(openai_api, "post_json", lambda *a, **k: (200, "not json"))
    with pytest.raises(MailError, match="could not parse"):
        openai_api.call(CFG, "system", "user", SCHEMA)


def test_call_uses_headers_not_query_string(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen = {}

    def fake_post_json(url, payload, headers=None, timeout=30):
        seen["url"] = url
        seen["headers"] = headers
        return 200, _reply([])

    monkeypatch.setattr(openai_api, "post_json", fake_post_json)
    openai_api.call(CFG, "system", "user", SCHEMA)
    assert "sk-test" not in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer sk-test"


def test_call_honors_model_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen = {}

    def fake_post_json(url, payload, headers=None, timeout=30):
        seen["payload"] = payload
        return 200, _reply([])

    monkeypatch.setattr(openai_api, "post_json", fake_post_json)
    cfg = Config(delivery="email", model="gpt-custom")
    openai_api.call(cfg, "system", "user", SCHEMA)
    assert seen["payload"]["model"] == "gpt-custom"
