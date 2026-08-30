"""codex_cli.call: subprocess.run monkeypatched, HOME pointed at tmp_path so
auth.json writes land somewhere disposable."""

from __future__ import annotations

import json
import stat

import pytest

from mailtriage.config import Config
from mailtriage.errors import MailError
from mailtriage.triage import codex_cli

CFG = Config(delivery="email")
SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}


class _StubCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_call_happy_path_writes_out_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}", encoding="utf-8")

    def fake_run(args, **kwargs):
        out_path = args[args.index("--output-last-message") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"items": [{"id": 0, "bucket": "needs_action", "note": "via codex"}]}, f)
        return _StubCompletedProcess(0)

    monkeypatch.setattr("mailtriage.triage.codex_cli.subprocess.run", fake_run)
    result = codex_cli.call(CFG, "system", "user", SCHEMA)
    assert result == {"items": [{"id": 0, "bucket": "needs_action", "note": "via codex"}]}


def test_call_missing_cli_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}", encoding="utf-8")

    def raise_not_found(*a, **k):
        raise FileNotFoundError("no such file: codex")

    monkeypatch.setattr("mailtriage.triage.codex_cli.subprocess.run", raise_not_found)
    with pytest.raises(MailError, match="not installed"):
        codex_cli.call(CFG, "system", "user", SCHEMA)


def test_call_nonzero_exit_with_401_mentions_repasting_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "mailtriage.triage.codex_cli.subprocess.run",
        lambda *a, **k: _StubCompletedProcess(1, stderr="Error: 401 Unauthorized"),
    )
    with pytest.raises(MailError, match="re-paste"):
        codex_cli.call(CFG, "system", "user", SCHEMA)


def test_call_no_auth_configured_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_AUTH_JSON", raising=False)
    with pytest.raises(MailError, match="codex login"):
        codex_cli.call(CFG, "system", "user", SCHEMA)


def test_auth_json_written_from_env_var_with_0600(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_AUTH_JSON", '{"token": "secret"}')

    def fake_run(args, **kwargs):
        out_path = args[args.index("--output-last-message") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"items": []}, f)
        return _StubCompletedProcess(0)

    monkeypatch.setattr("mailtriage.triage.codex_cli.subprocess.run", fake_run)
    codex_cli.call(CFG, "system", "user", SCHEMA)

    written = tmp_path / ".codex" / "auth.json"
    assert written.read_text(encoding="utf-8") == '{"token": "secret"}'
    mode = stat.S_IMODE(written.stat().st_mode)
    assert mode == 0o600
