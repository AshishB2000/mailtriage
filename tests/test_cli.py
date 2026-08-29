"""cli.main is the only place errors become stderr+exit. --self-check must work
with anthropic uninstalled, so this must never import triage/delivery eagerly.
"""

from __future__ import annotations

from typing import Any

from mailtriage.cli import main, run
from mailtriage.config import Config
from mailtriage.models import Email, Triaged


def test_self_check_exits_zero(capsys: Any) -> None:
    assert main(["--self-check"]) == 0
    assert "self-check: ok" in capsys.readouterr().out


def test_version() -> None:
    assert main(["--version"]) == 0


def test_bad_config_returns_1(capsys: Any, tmp_path: Any) -> None:
    missing = tmp_path / "nope.yaml"
    assert main(["--config", str(missing)]) == 1
    err = capsys.readouterr().err
    assert "mailtriage:" in err
    assert "Traceback" not in err


def _email(i: int) -> Email:
    return {
        "account": "acct",
        "from": f"sender{i}@example.com",
        "subject": f"subject-{i}",
        "snippet": f"snippet-{i}",
        "date": "2026-08-28T10:00:00+00:00",
        "unread": False,
        "link": f"https://real.example.com/{i}",
    }


def _triaged(i: int) -> Triaged:
    return {
        "bucket": "needs_action",
        "note": "reply to this",
        "account": "acct",
        "sender": f"sender{i}@example.com",
        "subject": f"subject-{i}",
        "link": f"https://real.example.com/{i}",
        "date": "2026-08-28T10:00:00+00:00",
        "unread": False,
    }


def test_dry_run_prints_not_sends(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    monkeypatch.setattr(cli_module, "pull", lambda environ, now, hours: {"messages": [_email(0)], "warnings": []})
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [_triaged(0)])

    def _boom(cfg: Config, kept: list[Triaged]) -> None:
        raise AssertionError("send must not be called on a dry run")

    monkeypatch.setattr(delivery_module, "send", _boom)

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com")
    run(cfg, dry_run=True)

    out = capsys.readouterr().out
    assert "subject-0" in out
    assert "reply to this" in out
