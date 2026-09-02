"""gemini_cli.call: subprocess.run monkeypatched, HOME pointed at tmp_path so
oauth_creds.json writes land somewhere disposable."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from mailtriage.config import Config
from mailtriage.errors import MailError
from mailtriage.triage import gemini_cli

CFG = Config(delivery="email")
SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}
ITEMS = {"items": [{"id": 0, "bucket": "needs_action", "note": "via gemini"}]}


class _StubCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _seed_creds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "oauth_creds.json").write_text("{}", encoding="utf-8")


def _envelope(response_text: str, returncode: int = 0) -> str:
    return json.dumps({"response": response_text, "stats": {}})


def test_call_happy_path_fenced_json(monkeypatch, tmp_path):
    _seed_creds(tmp_path, monkeypatch)

    def fake_run(args, **kwargs):
        text = "```json\n" + json.dumps(ITEMS) + "\n```"
        return _StubCompletedProcess(0, stdout=_envelope(text))

    monkeypatch.setattr("mailtriage.triage.gemini_cli.subprocess.run", fake_run)
    result = gemini_cli.call(CFG, "system", "user", SCHEMA)
    assert result == ITEMS


def test_call_unfenced_json(monkeypatch, tmp_path):
    _seed_creds(tmp_path, monkeypatch)

    def fake_run(args, **kwargs):
        return _StubCompletedProcess(0, stdout=_envelope(json.dumps(ITEMS)))

    monkeypatch.setattr("mailtriage.triage.gemini_cli.subprocess.run", fake_run)
    result = gemini_cli.call(CFG, "system", "user", SCHEMA)
    assert result == ITEMS


def test_call_json_with_surrounding_prose(monkeypatch, tmp_path):
    _seed_creds(tmp_path, monkeypatch)

    def fake_run(args, **kwargs):
        text = "Sure, here you go:\n" + json.dumps(ITEMS) + "\nHope that helps!"
        return _StubCompletedProcess(0, stdout=_envelope(text))

    monkeypatch.setattr("mailtriage.triage.gemini_cli.subprocess.run", fake_run)
    result = gemini_cli.call(CFG, "system", "user", SCHEMA)
    assert result == ITEMS


def test_call_missing_cli_raises(monkeypatch, tmp_path):
    _seed_creds(tmp_path, monkeypatch)

    def raise_not_found(*a, **k):
        raise FileNotFoundError("no such file: gemini")

    monkeypatch.setattr("mailtriage.triage.gemini_cli.subprocess.run", raise_not_found)
    with pytest.raises(MailError, match="not installed"):
        gemini_cli.call(CFG, "system", "user", SCHEMA)


def test_call_exit_41_mentions_repasting_oauth_creds(monkeypatch, tmp_path):
    _seed_creds(tmp_path, monkeypatch)

    def fake_run(args, **kwargs):
        envelope = {"response": "", "error": {"type": "AuthenticationError", "message": "token expired"}}
        return _StubCompletedProcess(41, stdout=json.dumps(envelope))

    monkeypatch.setattr("mailtriage.triage.gemini_cli.subprocess.run", fake_run)
    with pytest.raises(MailError, match="re-paste"):
        gemini_cli.call(CFG, "system", "user", SCHEMA)
    with pytest.raises(MailError, match="oauth_creds.json"):
        gemini_cli.call(CFG, "system", "user", SCHEMA)


def test_call_envelope_error_raises(monkeypatch, tmp_path):
    _seed_creds(tmp_path, monkeypatch)

    def fake_run(args, **kwargs):
        envelope = {"response": "", "error": {"type": "InputError", "message": "bad input", "code": 42}}
        return _StubCompletedProcess(42, stdout=json.dumps(envelope))

    monkeypatch.setattr("mailtriage.triage.gemini_cli.subprocess.run", fake_run)
    with pytest.raises(MailError, match="bad input"):
        gemini_cli.call(CFG, "system", "user", SCHEMA)


def test_call_no_auth_configured_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.delenv("GEMINI_OAUTH_JSON", raising=False)
    with pytest.raises(MailError, match="gemini"):
        gemini_cli.call(CFG, "system", "user", SCHEMA)


def test_oauth_creds_written_from_env_var_with_0600_and_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.setenv("GEMINI_OAUTH_JSON", '{"refresh_token": "secret"}')
    monkeypatch.setenv("GEMINI_API_KEY", "should-be-removed")

    captured_env = {}

    def fake_run(args, **kwargs):
        captured_env.update(kwargs["env"])
        return _StubCompletedProcess(0, stdout=_envelope(json.dumps(ITEMS)))

    monkeypatch.setattr("mailtriage.triage.gemini_cli.subprocess.run", fake_run)
    gemini_cli.call(CFG, "system", "user", SCHEMA)

    creds = tmp_path / ".gemini" / "oauth_creds.json"
    assert creds.read_text(encoding="utf-8") == '{"refresh_token": "secret"}'
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600

    settings = json.loads((tmp_path / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert settings == {"security": {"auth": {"selectedType": "oauth-personal"}}}

    assert captured_env["GOOGLE_GENAI_USE_GCA"] == "true"
    assert "GEMINI_API_KEY" not in captured_env


def test_existing_settings_json_not_clobbered(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_CLI_HOME", raising=False)
    monkeypatch.setenv("GEMINI_OAUTH_JSON", '{"refresh_token": "secret"}')
    gemini_dir = tmp_path / ".gemini"
    gemini_dir.mkdir()
    gemini_dir.joinpath("settings.json").write_text('{"custom": true}', encoding="utf-8")

    def fake_run(args, **kwargs):
        return _StubCompletedProcess(0, stdout=_envelope(json.dumps(ITEMS)))

    monkeypatch.setattr("mailtriage.triage.gemini_cli.subprocess.run", fake_run)
    gemini_cli.call(CFG, "system", "user", SCHEMA)

    assert json.loads(gemini_dir.joinpath("settings.json").read_text(encoding="utf-8")) == {"custom": True}


def test_model_override_passes_dash_m(monkeypatch, tmp_path):
    _seed_creds(tmp_path, monkeypatch)
    cfg = Config(delivery="email", model="gemini-3.0-pro")

    captured_args = []

    def fake_run(args, **kwargs):
        captured_args.extend(args)
        return _StubCompletedProcess(0, stdout=_envelope(json.dumps(ITEMS)))

    monkeypatch.setattr("mailtriage.triage.gemini_cli.subprocess.run", fake_run)
    gemini_cli.call(cfg, "system", "user", SCHEMA)

    assert "-m" in captured_args
    assert captured_args[captured_args.index("-m") + 1] == "gemini-3.0-pro"
