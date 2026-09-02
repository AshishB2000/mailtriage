"""cli.main is the only place errors become stderr+exit. --self-check must work
with anthropic uninstalled, so this must never import triage/delivery eagerly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import yaml

from mailtriage.cli import main, run
from mailtriage.config import Config
from mailtriage.models import Email, Triaged


def _write_config(tmp_path: Any, **overrides: Any) -> Any:
    data = {"delivery": "email", "run_at": ["08:00"], "timezone": "UTC", **overrides}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


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
        "body": f"body-{i}",
        "date": "2026-08-28T10:00:00+00:00",
        "unread": False,
        "link": f"https://real.example.com/{i}",
        "message_id": f"<msg-{i}@example.com>",
        "reply_to": f"sender{i}@example.com",
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
        "idx": i,
        "draft": "",
    }


def test_dry_run_prints_not_sends(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    monkeypatch.setattr(cli_module, "pull", lambda environ, now, hours: {"messages": [_email(0)], "warnings": []})
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [_triaged(0)])
    monkeypatch.setattr(
        triage_module, "select_backend", lambda cfg, environ: ("stub", lambda cfg, s, u, schema: {"items": []})
    )

    def _boom_send(cfg: Config, kept: list[Triaged]) -> None:
        raise AssertionError("send must not be called on a dry run")

    def _boom_push(environ: Any, kept: list[Triaged], emails: list[Email]) -> list[dict[str, str]]:
        raise AssertionError("push_drafts must not be called on a dry run")

    monkeypatch.setattr(delivery_module, "send", _boom_send)
    monkeypatch.setattr(cli_module, "push_drafts", _boom_push)

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com")
    run(cfg, dry_run=True)

    out = capsys.readouterr().out
    assert "subject-0" in out
    assert "reply to this" in out


def test_dry_run_prints_drafts(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    monkeypatch.setattr(cli_module, "pull", lambda environ, now, hours: {"messages": [_email(0)], "warnings": []})
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [_triaged(0)])

    def fake_call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {"items": [{"id": 0, "draft": "Sounds good, see you then."}]}

    monkeypatch.setattr(triage_module, "select_backend", lambda cfg, environ: ("stub", fake_call))
    monkeypatch.setattr(delivery_module, "send", lambda cfg, kept: (_ for _ in ()).throw(AssertionError("no send")))
    monkeypatch.setattr(
        cli_module, "push_drafts", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no push on dry run"))
    )

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com")
    run(cfg, dry_run=True)

    out = capsys.readouterr().out
    assert "Sounds good, see you then." in out


def test_run_pushes_drafts_and_prints_push_warnings_then_still_delivers(monkeypatch: Any, capsys: Any) -> None:
    """A real (non-dry) run must draft, attempt the Gmail-Drafts push, print any
    push warning like a dead-feed warning (never raise), and still deliver."""
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    monkeypatch.setattr(cli_module, "pull", lambda environ, now, hours: {"messages": [_email(0)], "warnings": []})
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [_triaged(0)])

    def fake_call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {"items": [{"id": 0, "draft": "a draft"}]}

    monkeypatch.setattr(triage_module, "select_backend", lambda cfg, environ: ("stub", fake_call))

    push_calls: list[Any] = []

    def fake_push_drafts(environ: Any, kept: list[Triaged], emails: list[Email]) -> list[dict[str, str]]:
        push_calls.append(kept)
        return [{"account": "acct", "error": "boom"}]

    monkeypatch.setattr(cli_module, "push_drafts", fake_push_drafts)

    sent: list[Any] = []
    monkeypatch.setattr(delivery_module, "send", lambda cfg, kept: sent.append(kept))

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com")
    run(cfg, dry_run=False)

    assert len(push_calls) == 1
    assert push_calls[0][0]["draft"] == "a draft"
    assert len(sent) == 1
    assert "draft push failed, skipping" in capsys.readouterr().err


def test_rule_forced_item_produces_a_digest_even_when_model_kept_nothing(monkeypatch: Any, capsys: Any) -> None:
    """always_action must survive the 'model kept none' early return -- see
    rules.enforce() and its wiring in cli.run()."""
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    boss_email = {**_email(0), "from": "boss@corp.com"}
    monkeypatch.setattr(cli_module, "pull", lambda environ, now, hours: {"messages": [boss_email], "warnings": []})
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [])  # model kept nothing
    monkeypatch.setattr(
        triage_module, "select_backend", lambda cfg, environ: ("stub", lambda cfg, s, u, schema: {"items": []})
    )

    sent: list[Any] = []
    monkeypatch.setattr(delivery_module, "send", lambda cfg, kept: sent.append(kept))

    cfg = Config(
        delivery="email",
        email_to="me@example.com",
        email_from="bot@example.com",
        draft_replies=False,
        rules={"always_ignore": [], "always_surface": [], "always_action": ["boss@corp.com"]},
    )
    run(cfg, dry_run=False)

    assert len(sent) == 1
    assert sent[0][0]["bucket"] == "needs_action"
    assert sent[0][0]["note"] == "rule: always action from boss@corp.com"
    assert "the model kept none" not in capsys.readouterr().err


# --- --due -------------------------------------------------------------


def test_due_exits_0_and_never_calls_pull(monkeypatch: Any, tmp_path: Any) -> None:
    import mailtriage.cli as cli_module

    cfg_path = _write_config(tmp_path)  # run_at: ["08:00"]
    monkeypatch.setattr(cli_module, "_now", lambda: datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc))
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setattr(
        cli_module, "pull", lambda *a, **k: (_ for _ in ()).throw(AssertionError("pull must not run for --due"))
    )

    assert main(["--due", "--config", str(cfg_path)]) == 0


def test_due_exits_3_and_prints_not_due_message(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module

    cfg_path = _write_config(tmp_path)  # run_at: ["08:00"]
    monkeypatch.setattr(cli_module, "_now", lambda: datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc))
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setattr(
        cli_module, "pull", lambda *a, **k: (_ for _ in ()).throw(AssertionError("pull must not run for --due"))
    )

    assert main(["--due", "--config", str(cfg_path)]) == 3
    err = capsys.readouterr().err
    assert "not due" in err
    assert "08:00" in err


def test_due_weekly_slot_exits_3_with_not_implemented_message(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module

    cfg_path = _write_config(tmp_path, run_at=["06:00"], weekly_review="thu 09:00")
    monkeypatch.setattr(cli_module, "_now", lambda: datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc))  # a Thursday
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setattr(
        cli_module, "pull", lambda *a, **k: (_ for _ in ()).throw(AssertionError("pull must not run for --due"))
    )

    assert main(["--due", "--config", str(cfg_path)]) == 3
    assert "weekly review slot (not implemented yet)" in capsys.readouterr().err


def test_due_workflow_dispatch_always_due_regardless_of_time(monkeypatch: Any, tmp_path: Any) -> None:
    import mailtriage.cli as cli_module

    cfg_path = _write_config(tmp_path)  # run_at: ["08:00"]
    monkeypatch.setattr(cli_module, "_now", lambda: datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")

    assert main(["--due", "--config", str(cfg_path)]) == 0


def test_due_bad_config_returns_1_not_3(tmp_path: Any, capsys: Any) -> None:
    missing = tmp_path / "nope.yaml"
    assert main(["--due", "--config", str(missing)]) == 1
    assert "mailtriage:" in capsys.readouterr().err
