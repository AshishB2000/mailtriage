"""gemini_api.call, transport monkeypatched at the module's post_json seam."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mailtriage.config import Config
from mailtriage.errors import MailError
from mailtriage.triage import gemini_api

CFG = Config(delivery="email")
SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "object", "properties": {"id": {"type": "integer"}}, "additionalProperties": False},
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _reply(items: list[dict[str, Any]]) -> str:
    return json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps({"items": items})}]}}]})


def test_call_missing_key_raises(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(MailError, match="GEMINI_API_KEY"):
        gemini_api.call(CFG, "system", "user", SCHEMA)


def test_call_happy_path(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setattr(gemini_api, "post_json", lambda *a, **k: (200, _reply([{"id": 0, "bucket": "noise"}])))
    result = gemini_api.call(CFG, "system", "user", SCHEMA)
    assert result == {"items": [{"id": 0, "bucket": "noise"}]}


def test_call_bad_key_raises(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-bad")
    monkeypatch.setattr(gemini_api, "post_json", lambda *a, **k: (400, '{"error":{"message":"API_KEY_INVALID"}}'))
    with pytest.raises(MailError, match="GEMINI_API_KEY"):
        gemini_api.call(CFG, "system", "user", SCHEMA)


def test_call_403_forbidden_raises(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-bad")
    monkeypatch.setattr(
        gemini_api, "post_json", lambda *a, **k: (403, '{"error":{"message":"PERMISSION_DENIED: API_KEY"}}')
    )
    with pytest.raises(MailError, match="GEMINI_API_KEY"):
        gemini_api.call(CFG, "system", "user", SCHEMA)


def test_call_429_raises(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setattr(gemini_api, "post_json", lambda *a, **k: (429, "{}"))
    with pytest.raises(MailError, match="rate-limited"):
        gemini_api.call(CFG, "system", "user", SCHEMA)


def test_call_malformed_body_raises(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setattr(gemini_api, "post_json", lambda *a, **k: (200, "not json"))
    with pytest.raises(MailError, match="could not parse"):
        gemini_api.call(CFG, "system", "user", SCHEMA)


def test_call_key_in_header_not_url_and_schema_has_no_additional_properties(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    seen = {}

    def fake_post_json(url, payload, headers=None, timeout=30):
        seen["url"] = url
        seen["headers"] = headers
        seen["payload"] = payload
        return 200, _reply([])

    monkeypatch.setattr(gemini_api, "post_json", fake_post_json)
    gemini_api.call(CFG, "system", "user", SCHEMA)

    assert "g-test" not in seen["url"]
    assert seen["headers"]["x-goog-api-key"] == "g-test"

    def has_additional_properties(node: Any) -> bool:
        if isinstance(node, dict):
            if "additionalProperties" in node:
                return True
            return any(has_additional_properties(v) for v in node.values())
        if isinstance(node, list):
            return any(has_additional_properties(v) for v in node)
        return False

    assert not has_additional_properties(seen["payload"])


def test_call_honors_model_override(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    seen = {}

    def fake_post_json(url, payload, headers=None, timeout=30):
        seen["url"] = url
        return 200, _reply([])

    monkeypatch.setattr(gemini_api, "post_json", fake_post_json)
    cfg = Config(delivery="email", model="gemini-custom")
    gemini_api.call(cfg, "system", "user", SCHEMA)
    assert "gemini-custom" in seen["url"]


def test_strip_additional_properties_helper():
    nested = {"a": {"additionalProperties": False, "b": [{"additionalProperties": True}, 1, "x"]}}
    stripped = gemini_api._strip_additional_properties(nested)
    assert stripped == {"a": {"b": [{}, 1, "x"]}}
