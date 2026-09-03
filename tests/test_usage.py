"""The usage line every backend prints after a model call: counts only."""

from __future__ import annotations

import json
from typing import Any

import anthropic

from mailtriage.config import Config
from mailtriage.triage import claude_api, claude_cli, gemini_api, gemini_cli, openai_api
from mailtriage.triage.usage import log_usage

CFG = Config(delivery="email")
SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}


class _Proc:
    def __init__(self, stdout: str) -> None:
        self.returncode, self.stdout, self.stderr = 0, stdout, ""


def test_log_usage_formats_cost_and_skips_when_counts_missing(capsys: Any) -> None:
    log_usage(1200, 34, 0.01234)
    log_usage(10, 2)
    log_usage(None, 2)
    log_usage("10", 2, 0.1)
    err = capsys.readouterr().err.splitlines()
    assert err == ["mailtriage: usage input=1200 output=34 cost=$0.0123", "mailtriage: usage input=10 output=2"]


def test_claude_cli_prints_usage_from_envelope(monkeypatch: Any, capsys: Any) -> None:
    env = {
        "structured_output": {"items": []},
        "usage": {"input_tokens": 900, "output_tokens": 40},
        "total_cost_usd": 0.0071,
    }
    monkeypatch.setattr("mailtriage.triage.claude_cli.subprocess.run", lambda *a, **k: _Proc(json.dumps(env)))
    claude_cli.call(CFG, "s", "u", SCHEMA)
    assert "mailtriage: usage input=900 output=40 cost=$0.0071" in capsys.readouterr().err


def test_claude_api_prints_usage(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    class Usage:
        input_tokens, output_tokens = 500, 20

    class Block:
        type = "tool_use"

        def __init__(self) -> None:
            self.input: dict[str, Any] = {"items": []}

    class Resp:
        stop_reason, content, usage = "end_turn", [Block()], Usage()

    class Client:
        class messages:
            @staticmethod
            def create(**kw: Any) -> Resp:
                return Resp()

    monkeypatch.setattr(anthropic, "Anthropic", lambda: Client())
    claude_api.call(CFG, "s", "u", SCHEMA)
    assert "mailtriage: usage input=500 output=20\n" in capsys.readouterr().err


def test_openai_api_prints_usage(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    body = {
        "choices": [{"message": {"content": json.dumps({"items": []})}}],
        "usage": {"prompt_tokens": 700, "completion_tokens": 30},
    }
    monkeypatch.setattr(openai_api, "post_json", lambda *a, **k: (200, json.dumps(body)))
    openai_api.call(CFG, "s", "u", SCHEMA)
    assert "mailtriage: usage input=700 output=30\n" in capsys.readouterr().err


def test_gemini_api_prints_usage(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    body = {
        "candidates": [{"content": {"parts": [{"text": json.dumps({"items": []})}]}}],
        "usageMetadata": {"promptTokenCount": 650, "candidatesTokenCount": 25},
    }
    monkeypatch.setattr(gemini_api, "post_json", lambda *a, **k: (200, json.dumps(body)))
    gemini_api.call(CFG, "s", "u", SCHEMA)
    assert "mailtriage: usage input=650 output=25\n" in capsys.readouterr().err


def test_gemini_cli_prints_usage_summed_across_models(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "oauth_creds.json").write_text("{}")
    env = {
        "response": json.dumps({"items": []}),
        "stats": {
            "models": {
                "gemini-2.5-flash": {"tokens": {"prompt": 600, "candidates": 20}},
                "gemini-2.5-flash-lite": {"tokens": {"prompt": 100, "candidates": 5}},
            }
        },
    }
    monkeypatch.setattr("mailtriage.triage.gemini_cli.subprocess.run", lambda *a, **k: _Proc(json.dumps(env)))
    gemini_cli.call(CFG, "s", "u", SCHEMA)
    assert "mailtriage: usage input=700 output=25\n" in capsys.readouterr().err


def test_no_usage_line_when_backend_exposes_none(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(
        "mailtriage.triage.claude_cli.subprocess.run",
        lambda *a, **k: _Proc(json.dumps({"structured_output": {"items": []}})),
    )
    claude_cli.call(CFG, "s", "u", SCHEMA)
    assert "usage" not in capsys.readouterr().err
