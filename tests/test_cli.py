"""cli.main is the only place errors become stderr+exit. --self-check must work
with anthropic uninstalled, so this must never import triage/delivery eagerly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
import yaml

from mailtriage.cli import main, run, run_weekly
from mailtriage.config import Config
from mailtriage.models import Email, Triaged, WeekResult


@pytest.fixture(autouse=True)
def _no_real_provider(monkeypatch: Any) -> None:
    """Belt and braces: whatever secrets the developer's shell exports, no
    test in this file may reach a real model. Tests that care stub
    select_backend themselves, on top of this."""
    import mailtriage.triage as triage_module

    monkeypatch.setattr(
        triage_module, "select_backend", lambda cfg, environ: ("stub", lambda cfg, s, u, schema: {"items": []})
    )


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
        "uid": f"{i}",
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

    monkeypatch.setattr(
        cli_module, "pull", lambda environ, now, hours, only=None: {"messages": [_email(0)], "warnings": []}
    )
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [_triaged(0)])
    monkeypatch.setattr(
        triage_module, "select_backend", lambda cfg, environ: ("stub", lambda cfg, s, u, schema: {"items": []})
    )

    def _boom_send(cfg: Config, kept: list[Triaged], stamp: str = "", events: Any = None) -> None:
        raise AssertionError("send must not be called on a dry run")

    def _boom_push(environ: Any, kept: list[Triaged], emails: list[Email]) -> list[dict[str, str]]:
        raise AssertionError("push_drafts must not be called on a dry run")

    monkeypatch.setattr(delivery_module, "send", _boom_send)
    monkeypatch.setattr(cli_module, "push_drafts", _boom_push)

    # carry_over is unrelated to what this test checks -- off, so it never has
    # to touch IMAP (real MAIL_ACCOUNTS isn't set for this test process).
    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com", carry_over=False)
    run(cfg, dry_run=True)

    out = capsys.readouterr().out
    assert "subject-0" in out
    assert "reply to this" in out


def test_dry_run_prints_drafts(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    monkeypatch.setattr(
        cli_module, "pull", lambda environ, now, hours, only=None: {"messages": [_email(0)], "warnings": []}
    )
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [_triaged(0)])

    def fake_call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {"items": [{"id": 0, "draft": "Sounds good, see you then."}]}

    monkeypatch.setattr(triage_module, "select_backend", lambda cfg, environ: ("stub", fake_call))
    monkeypatch.setattr(
        delivery_module,
        "send",
        lambda cfg, kept, stamp="", events=None: (_ for _ in ()).throw(AssertionError("no send")),
    )
    monkeypatch.setattr(
        cli_module, "push_drafts", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no push on dry run"))
    )

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com", carry_over=False)
    run(cfg, dry_run=True)

    out = capsys.readouterr().out
    assert "Sounds good, see you then." in out


def test_run_pushes_drafts_and_prints_push_warnings_then_still_delivers(monkeypatch: Any, capsys: Any) -> None:
    """A real (non-dry) run must draft, attempt the Gmail-Drafts push, print any
    push warning like a dead-feed warning (never raise), and still deliver."""
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    monkeypatch.setattr(
        cli_module, "pull", lambda environ, now, hours, only=None: {"messages": [_email(0)], "warnings": []}
    )
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
    monkeypatch.setattr(delivery_module, "send", lambda cfg, kept, stamp="", events=None: sent.append(kept))

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com", carry_over=False)
    run(cfg, dry_run=False)

    assert len(push_calls) == 1
    assert push_calls[0][0]["draft"] == "a draft"
    assert len(sent) == 1
    assert "draft push failed, skipping" in capsys.readouterr().err


def _open_action_email(i: int) -> Email:
    return {
        "account": "acct",
        "from": f"open{i}@example.com",
        "subject": f"open action {i}",
        "snippet": "",
        "body": "",
        "date": "2026-08-20T10:00:00+00:00",
        "unread": True,
        "link": f"https://real.example.com/open{i}",
        "message_id": f"<open-{i}@example.com>",
        "reply_to": f"open{i}@example.com",
        "uid": f"open{i}",
    }


def test_carried_alone_never_triggers_a_digest(monkeypatch: Any, capsys: Any) -> None:
    """The empty-digest check runs on the model's kept list before any
    carry-over items are merged in, so yesterday's open items never conjure a
    digest out of thin air on their own."""
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    monkeypatch.setattr(
        cli_module, "pull", lambda environ, now, hours, only=None: {"messages": [_email(0)], "warnings": []}
    )
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [])  # nothing new kept

    def _boom_open_actions(*a: Any, **k: Any) -> Any:
        raise AssertionError("pull_open_actions must not run when there are no new kept items")

    monkeypatch.setattr(cli_module, "pull_open_actions", _boom_open_actions)
    monkeypatch.setattr(
        delivery_module,
        "send",
        lambda cfg, kept, stamp="", events=None: (_ for _ in ()).throw(AssertionError("no send")),
    )

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com", carry_over=True)
    run(cfg, dry_run=False)

    assert "sending nothing" in capsys.readouterr().err


def test_new_and_carried_both_appear_in_the_digest(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    monkeypatch.setattr(
        cli_module, "pull", lambda environ, now, hours, only=None: {"messages": [_email(0)], "warnings": []}
    )
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [_triaged(0)])
    monkeypatch.setattr(
        triage_module, "select_backend", lambda cfg, environ: ("stub", lambda cfg, s, u, schema: {"items": []})
    )
    monkeypatch.setattr(cli_module, "label_actions", lambda *a, **k: [])
    monkeypatch.setattr(
        cli_module, "pull_open_actions", lambda *a, **k: {"messages": [_open_action_email(0)], "warnings": []}
    )
    monkeypatch.setattr(
        delivery_module,
        "send",
        lambda cfg, kept, stamp="", events=None: (_ for _ in ()).throw(AssertionError("no send")),
    )

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com", carry_over=True)
    run(cfg, dry_run=True)

    out = capsys.readouterr().out
    assert "subject-0" in out
    assert "open action 0" in out
    assert "Still waiting on you" in out


def test_dry_run_reads_carried_but_never_labels(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    monkeypatch.setattr(
        cli_module, "pull", lambda environ, now, hours, only=None: {"messages": [_email(0)], "warnings": []}
    )
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [_triaged(0)])
    monkeypatch.setattr(
        triage_module, "select_backend", lambda cfg, environ: ("stub", lambda cfg, s, u, schema: {"items": []})
    )

    def _boom_label(*a: Any, **k: Any) -> Any:
        raise AssertionError("label_actions must not run on a dry run")

    monkeypatch.setattr(cli_module, "label_actions", _boom_label)

    open_calls: list[Any] = []

    def fake_open_actions(*a: Any, **k: Any) -> dict[str, Any]:
        open_calls.append(a)
        return {"messages": [], "warnings": []}

    monkeypatch.setattr(cli_module, "pull_open_actions", fake_open_actions)
    monkeypatch.setattr(
        delivery_module,
        "send",
        lambda cfg, kept, stamp="", events=None: (_ for _ in ()).throw(AssertionError("no send")),
    )

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com", carry_over=True)
    run(cfg, dry_run=True)

    assert len(open_calls) == 1  # reading is fine on a dry run -- only the write (label) is skipped


def test_rule_forced_item_produces_a_digest_even_when_model_kept_nothing(monkeypatch: Any, capsys: Any) -> None:
    """always_action must survive the 'model kept none' early return -- see
    rules.enforce() and its wiring in cli.run()."""
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    boss_email = {**_email(0), "from": "boss@corp.com"}
    monkeypatch.setattr(
        cli_module, "pull", lambda environ, now, hours, only=None: {"messages": [boss_email], "warnings": []}
    )
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [])  # model kept nothing
    monkeypatch.setattr(
        triage_module, "select_backend", lambda cfg, environ: ("stub", lambda cfg, s, u, schema: {"items": []})
    )

    sent: list[Any] = []
    monkeypatch.setattr(delivery_module, "send", lambda cfg, kept, stamp="", events=None: sent.append(kept))

    cfg = Config(
        delivery="email",
        email_to="me@example.com",
        email_from="bot@example.com",
        draft_replies=False,
        carry_over=False,
        rules={"always_ignore": [], "always_surface": [], "always_action": ["boss@corp.com"]},
    )
    run(cfg, dry_run=False)

    assert len(sent) == 1
    assert sent[0][0]["bucket"] == "needs_action"
    assert sent[0][0]["note"] == "rule: always action from boss@corp.com"
    assert "the model kept none" not in capsys.readouterr().err


def _write_config(tmp_path: Any, **overrides: Any) -> Any:
    data = {"delivery": "email", "run_at": ["08:00"], "timezone": "UTC", **overrides}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


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


def test_due_prints_digest_on_stdout_at_a_digest_slot(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module

    cfg_path = _write_config(tmp_path)  # run_at: ["08:00"]
    monkeypatch.setattr(cli_module, "_now", lambda: datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc))
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

    assert main(["--due", "--config", str(cfg_path)]) == 0
    assert capsys.readouterr().out.strip() == "digest"


def test_due_prints_weekly_on_stdout_at_a_weekly_slot(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module

    cfg_path = _write_config(tmp_path, run_at=["06:00"], weekly_review="thu 09:00")
    monkeypatch.setattr(cli_module, "_now", lambda: datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc))  # a Thursday
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

    assert main(["--due", "--config", str(cfg_path)]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "weekly"
    assert "weekly review slot" in out.err


def test_due_exits_3_and_prints_not_due_message(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module

    cfg_path = _write_config(tmp_path)  # run_at: ["08:00"]
    monkeypatch.setattr(cli_module, "_now", lambda: datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc))
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.setattr(
        cli_module, "pull", lambda *a, **k: (_ for _ in ()).throw(AssertionError("pull must not run for --due"))
    )

    assert main(["--due", "--config", str(cfg_path)]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout when not due
    assert "not due" in captured.err
    assert "08:00" in captured.err


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


# --- --weekly ------------------------------------------------------------


def _week_result(**accounts: dict[str, list[Any]]) -> WeekResult:
    return {"accounts": accounts, "warnings": []}


def _open_item(subject: str, age_days: int) -> dict[str, Any]:
    return {
        "account": "acct",
        "sender": "boss@example.com",
        "subject": subject,
        "date": "2026-08-20T10:00:00+00:00",
        "link": "https://real.example.com/x",
        "age_days": age_days,
    }


def test_weekly_dry_run_prints_summary(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module

    week = _week_result(
        acct={"replied": [], "archived": [], "open": [_open_item("still open", 4)]},
    )
    monkeypatch.setattr(cli_module, "pull_week", lambda environ, now, label, only=None: week)

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com")
    run_weekly(cfg, dry_run=True)

    out = capsys.readouterr().out
    assert "acct" in out
    assert "still open" in out
    assert "4d" in out


def test_weekly_sends_nothing_when_every_account_is_empty(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module

    week = _week_result(acct={"replied": [], "archived": [], "open": []})
    monkeypatch.setattr(cli_module, "pull_week", lambda environ, now, label, only=None: week)
    monkeypatch.setattr(delivery_module, "send_html", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no send")))

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com")
    run_weekly(cfg, dry_run=False)

    assert "sending nothing" in capsys.readouterr().err


def test_weekly_delivers_and_prints_handled_open_counts(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module

    week = _week_result(
        acct={
            "replied": [_open_item("r", 1)],
            "archived": [_open_item("a", 2)],
            "open": [_open_item("still open", 4)],
        },
    )
    monkeypatch.setattr(cli_module, "pull_week", lambda environ, now, label, only=None: week)

    sent: list[Any] = []
    monkeypatch.setattr(delivery_module, "send_html", lambda cfg, subject, html: sent.append((subject, html)))

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com", subject_prefix="mt")
    run_weekly(cfg, dry_run=False)

    assert len(sent) == 1
    assert sent[0][0] == "mt · weekly review"
    err = capsys.readouterr().err
    assert "weekly review delivered (2 handled, 1 open) via email." in err


def test_weekly_prints_account_warnings(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module

    week: WeekResult = {"accounts": {}, "warnings": [{"account": "acct", "error": "boom"}]}
    monkeypatch.setattr(cli_module, "pull_week", lambda environ, now, label, only=None: week)

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com")
    run_weekly(cfg, dry_run=False)

    assert "account failed, skipping" in capsys.readouterr().err


def test_main_weekly_flag_dispatches_run_weekly(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module

    cfg_path = _write_config(tmp_path)
    calls: list[Any] = []
    monkeypatch.setattr(cli_module, "run_weekly", lambda cfg, dry_run=False: calls.append((cfg.delivery, dry_run)))

    assert main(["--weekly", "--dry-run", "--config", str(cfg_path)]) == 0
    assert calls == [("email", True)]


def test_run_logs_candidate_count_without_subjects(monkeypatch: Any, capsys: Any) -> None:
    """Public forks have public Actions logs: report how many messages were
    pulled (the one fact that debugs 'kept none'), never what they were."""
    import mailtriage.cli as cli_module
    import mailtriage.triage as triage_module

    msgs = [_email(0), {**_email(1), "account": "other-acct"}]
    monkeypatch.setattr(cli_module, "pull", lambda environ, now, hours, only=None: {"messages": msgs, "warnings": []})
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [])

    run(Config(delivery="email", carry_over=False, window_hours=15), dry_run=True)

    err = capsys.readouterr().err
    assert "2 candidate(s) in the last 15h across 2 account(s)" in err
    assert "subject-0" not in err and "subject-1" not in err


# --- Gmail as the control plane (sub-project B) --------------------------


def _quiet_control_plane(monkeypatch: Any, cli_module: Any, **overrides: Any) -> None:
    """Stub every commands.* call cli.run makes to 'nothing happened', with
    overrides for the one under test."""
    labels = overrides.get("labels", {"counts": {"done": 0, "snoozed": 0, "woken": 0}, "skip": {}, "warnings": []})
    replies = overrides.get(
        "replies",
        {
            "replies": 0,
            "counts": dict.fromkeys(("done", "snooze", "draft", "never", "vip", "skipped"), 0),
            "skip_message_ids": set(),
            "warnings": [],
        },
    )
    senders = overrides.get("senders", {"never": set(), "vip": set(), "warnings": []})
    monkeypatch.setattr(cli_module, "apply_label_commands", lambda environ, today, label: labels)
    monkeypatch.setattr(cli_module, "handle_replies", lambda cfg, environ, now, today, backend: replies)
    monkeypatch.setattr(cli_module, "derive_sender_rules", lambda environ: senders)


def test_run_drops_done_snoozed_and_reply_messages_from_candidates(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    msgs = [_email(0), _email(1), _email(2)]  # uid "1" is done/snoozed; msg-2 is the reader's own reply
    monkeypatch.setattr(cli_module, "pull", lambda environ, now, hours, only=None: {"messages": msgs, "warnings": []})
    triaged_input: list[Any] = []

    def fake_triage(cfg: Config, emails: list[Email], now: Any) -> list[Triaged]:
        triaged_input.append(emails)
        return []

    monkeypatch.setattr(triage_module, "triage", fake_triage)
    monkeypatch.setattr(
        delivery_module,
        "send",
        lambda cfg, kept, stamp="", events=None: (_ for _ in ()).throw(AssertionError("no send")),
    )
    _quiet_control_plane(
        monkeypatch,
        cli_module,
        labels={"counts": {"done": 1, "snoozed": 0, "woken": 0}, "skip": {"acct": {"1"}}, "warnings": []},
        replies={
            "replies": 1,
            "counts": {"done": 1, "snooze": 0, "draft": 0, "never": 0, "vip": 0, "skipped": 0},
            "skip_message_ids": {"<msg-2@example.com>"},
            "warnings": [],
        },
    )

    run(Config(delivery="email", carry_over=False), dry_run=False)

    assert [e["uid"] for e in triaged_input[0]] == ["0"]
    err = capsys.readouterr().err
    assert "labels: 1 done, 0 snoozed, 0 woken." in err
    assert "1 digest repl(ies) handled (1 done)." in err
    assert "dropped 2." in err
    assert "subject-" not in err


def test_never_and_vip_labels_become_rules_for_this_run(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    msgs = [_email(0), {**_email(1), "from": "Boss <BOSS@corp.com>"}]
    monkeypatch.setattr(cli_module, "pull", lambda environ, now, hours, only=None: {"messages": msgs, "warnings": []})
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [])
    monkeypatch.setattr(
        triage_module, "select_backend", lambda cfg, environ: ("stub", lambda cfg, s, u, schema: {"items": []})
    )
    sent: list[Any] = []
    monkeypatch.setattr(delivery_module, "send", lambda cfg, kept, stamp="", events=None: sent.append(kept))
    _quiet_control_plane(
        monkeypatch, cli_module, senders={"never": {"sender0@example.com"}, "vip": {"boss@corp.com"}, "warnings": []}
    )

    run(Config(delivery="email", carry_over=False, draft_replies=False), dry_run=True)

    err = capsys.readouterr().err
    assert "rules.always_ignore dropped 1 message(s)." in err
    assert "1 never-sender(s), 1 vip-sender(s)." in err


def test_dry_run_skips_label_writes_and_reply_handling_but_still_derives_senders(monkeypatch: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.triage as triage_module

    monkeypatch.setattr(
        cli_module, "pull", lambda environ, now, hours, only=None: {"messages": [_email(0)], "warnings": []}
    )
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [])

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("no writes on a dry run")

    monkeypatch.setattr(cli_module, "apply_label_commands", boom)
    monkeypatch.setattr(cli_module, "handle_replies", boom)
    derived: list[Any] = []

    def fake_derive(environ: Any) -> dict[str, Any]:
        derived.append(1)
        return {"never": set(), "vip": set(), "warnings": []}

    monkeypatch.setattr(cli_module, "derive_sender_rules", fake_derive)

    run(Config(delivery="email", carry_over=False), dry_run=True)

    assert derived == [1]


def test_dry_run_prints_numbered_items_with_due_and_waiting(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    monkeypatch.setattr(
        cli_module, "pull", lambda environ, now, hours, only=None: {"messages": [_email(0)], "warnings": []}
    )
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [{**_triaged(0), "due": "2099-01-05"}])
    monkeypatch.setattr(
        triage_module, "select_backend", lambda cfg, environ: ("stub", lambda cfg, s, u, schema: {"items": []})
    )
    monkeypatch.setattr(
        cli_module, "pull_open_actions", lambda *a, **k: {"messages": [_open_action_email(0)], "warnings": []}
    )
    monkeypatch.setattr(
        delivery_module,
        "send",
        lambda cfg, kept, stamp="", events=None: (_ for _ in ()).throw(AssertionError("no send")),
    )
    _quiet_control_plane(monkeypatch, cli_module)

    run(Config(delivery="email", carry_over=True, nag_after_days=3), dry_run=True)

    out = capsys.readouterr().out
    assert "Needs action · Later" in out
    assert "#1 subject-0" in out and "Due 2099-01-05 · https://calendar.google.com/calendar/render?" in out
    assert "#2 open action 0" in out and "waiting" in out and "STILL OPEN" in out  # 2026-08-20 is long past


def _narrative_call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    assert "still open" in user  # the open item's subject reaches the model
    return {"summary": "You cleared two things. One is aging. Boss keeps waiting.", "patterns": ["Boss always waits"]}


def test_weekly_narrative_opens_the_review(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    week = _week_result(acct={"replied": [_open_item("r", 1)], "archived": [], "open": [_open_item("still open", 4)]})
    monkeypatch.setattr(cli_module, "pull_week", lambda environ, now, label, only=None: week)
    monkeypatch.setattr(cli_module, "count_done", lambda environ, now, only=None: {"done": 0, "warnings": []})
    monkeypatch.setattr(triage_module, "select_backend", lambda cfg, environ: ("stub", _narrative_call))
    sent: list[Any] = []
    monkeypatch.setattr(delivery_module, "send_html", lambda cfg, subject, html: sent.append(html))

    cfg = Config(delivery="email", email_to="me@example.com", email_from="bot@example.com")
    run_weekly(cfg, dry_run=False)
    assert "Boss keeps waiting." in sent[0] and "Boss always waits" in sent[0]
    assert sent[0].index("Boss keeps waiting.") < sent[0].index("1 replied")  # above the account blocks

    run_weekly(cfg, dry_run=True)
    out = capsys.readouterr().out
    assert out.index("Boss keeps waiting.") < out.index("  - Boss always waits") < out.index("acct —")


def test_weekly_narrative_failure_falls_back_to_the_plain_review(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module
    from mailtriage.errors import MailError

    week = _week_result(acct={"replied": [], "archived": [], "open": [_open_item("still open", 4)]})
    monkeypatch.setattr(cli_module, "pull_week", lambda environ, now, label, only=None: week)
    monkeypatch.setattr(cli_module, "count_done", lambda environ, now, only=None: {"done": 0, "warnings": []})

    def boom(cfg: Config, s: str, u: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise MailError("Anthropic rate-limited this run")

    monkeypatch.setattr(triage_module, "select_backend", lambda cfg, environ: ("stub", boom))
    sent: list[Any] = []
    monkeypatch.setattr(delivery_module, "send_html", lambda cfg, subject, html: sent.append(html))

    run_weekly(Config(delivery="email", email_to="me@example.com", email_from="bot@example.com"), dry_run=False)

    assert len(sent) == 1 and "still open" in sent[0]
    assert "weekly narrative failed, sending the plain review: Anthropic rate-limited" in capsys.readouterr().err


def test_weekly_narrative_off_makes_no_model_call(monkeypatch: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    week = _week_result(acct={"replied": [], "archived": [], "open": [_open_item("still open", 4)]})
    monkeypatch.setattr(cli_module, "pull_week", lambda environ, now, label, only=None: week)
    monkeypatch.setattr(cli_module, "count_done", lambda environ, now, only=None: {"done": 0, "warnings": []})
    monkeypatch.setattr(
        triage_module, "select_backend", lambda cfg, environ: (_ for _ in ()).throw(AssertionError("no provider"))
    )
    monkeypatch.setattr(delivery_module, "send_html", lambda cfg, subject, html: None)

    run_weekly(
        Config(delivery="email", email_to="me@example.com", email_from="bot@example.com", weekly_narrative=False),
        dry_run=False,
    )


def test_dry_run_prints_today_block_and_passes_events_to_send(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    monkeypatch.setattr(
        cli_module, "pull", lambda environ, now, hours, only=None: {"messages": [_email(0)], "warnings": []}
    )
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [_triaged(0)])
    monkeypatch.setattr(
        triage_module, "select_backend", lambda cfg, environ: ("stub", lambda cfg, s, u, schema: {"items": []})
    )
    events = [
        {
            "summary": "Standup",
            "location": "Room 4",
            "url": "",
            "start": "2026-08-28T09:00:00+00:00",
            "end": "2026-08-28T09:30:00+00:00",
            "all_day": False,
        }
    ]
    monkeypatch.setattr(cli_module, "today_events", lambda environ, cfg, now: events)
    sent: list[Any] = []
    monkeypatch.setattr(delivery_module, "send", lambda cfg, kept, stamp="", events=None: sent.append(events))
    _quiet_control_plane(monkeypatch, cli_module)

    run(Config(delivery="email", carry_over=False, draft_replies=False), dry_run=True)
    out = capsys.readouterr().out
    assert out.index("Today") < out.index("09:00–09:30 · Standup · Room 4") < out.index("#1 subject-0")

    run(Config(delivery="email", carry_over=False, draft_replies=False), dry_run=False)
    assert sent == [events]


def test_weekly_counts_done_labels(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module

    week = _week_result(acct={"replied": [], "archived": [], "open": []})
    monkeypatch.setattr(cli_module, "pull_week", lambda environ, now, label, only=None: week)
    monkeypatch.setattr(cli_module, "count_done", lambda environ, now, only=None: {"done": 3, "warnings": []})
    sent: list[Any] = []
    monkeypatch.setattr(delivery_module, "send_html", lambda cfg, subject, html: sent.append(html))

    run_weekly(Config(delivery="email", email_to="me@example.com", email_from="bot@example.com"), dry_run=False)

    assert len(sent) == 1 and "3 marked done" in sent[0]  # done alone is worth a roll-up
    assert "(0 handled, 3 done, 0 open)" in capsys.readouterr().err


# --- profiles ------------------------------------------------------------


def _profiled(tmp_path: Any, **overrides: Any) -> Any:
    data = {
        "delivery": "email",
        "subject_prefix": "mt",
        "run_at": ["08:00"],
        "timezone": "UTC",
        "carry_over": False,
        "draft_replies": False,
        "profiles": {
            "work": {"accounts": ["w@corp.com"], "delivery": "slack", "run_at": ["09:00"]},
            "home": {"accounts": ["h@gmail.com", "h2@gmail.com"], "run_at": ["20:00"]},
        },
        **overrides,
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")  # profiles run in file order
    return p


def _stub_profile_pipeline(monkeypatch: Any) -> tuple[list[Any], list[Any]]:
    """Returns (pulls, sends): the `only` set each pull() got, and
    (subject_prefix, delivery) per send()."""
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    pulls: list[Any] = []
    sends: list[Any] = []

    def fake_pull(environ: Any, now: Any, hours: int, only: Any = None) -> Any:
        pulls.append(only)
        return {"messages": [_email(0)], "warnings": []}

    monkeypatch.setattr(cli_module, "pull", fake_pull)
    monkeypatch.setattr(cli_module, "already_delivered", lambda *a, **k: False)
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [_triaged(0)])
    monkeypatch.setattr(
        triage_module, "select_backend", lambda cfg, environ: ("stub", lambda cfg, s, u, schema: {"items": []})
    )
    monkeypatch.setattr(
        delivery_module,
        "send",
        lambda cfg, kept, stamp="", events=None: sends.append((cfg.subject_prefix, cfg.delivery)),
    )
    return pulls, sends


def test_profiles_run_once_each_over_their_own_accounts(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    pulls, sends = _stub_profile_pipeline(monkeypatch)

    assert main(["--config", str(_profiled(tmp_path))]) == 0

    assert pulls == [{"w@corp.com"}, {"h@gmail.com", "h2@gmail.com"}]
    assert sends == [("mt · work", "slack"), ("mt · home", "email")]
    err = capsys.readouterr().err
    assert "profile work: digest over 1 account(s) via slack" in err
    assert "profile home: digest over 2 account(s) via email" in err


def test_scheduled_run_only_runs_the_due_profiles(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module

    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setattr(cli_module, "_now", lambda: datetime(2026, 1, 1, 9, 10, tzinfo=timezone.utc))
    pulls, sends = _stub_profile_pipeline(monkeypatch)

    assert main(["--config", str(_profiled(tmp_path))]) == 0

    assert pulls == [{"w@corp.com"}]
    assert sends == [("mt · work", "slack")]
    assert "profile home: not due" in capsys.readouterr().err


def test_due_is_due_when_any_profile_is(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module

    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    cfg_path = _profiled(tmp_path)  # base 08:00 (unused), work 09:00, home 20:00

    monkeypatch.setattr(cli_module, "_now", lambda: datetime(2026, 1, 1, 20, 10, tzinfo=timezone.utc))
    assert main(["--due", "--config", str(cfg_path)]) == 0
    assert capsys.readouterr().out.strip() == "digest"

    monkeypatch.setattr(cli_module, "_now", lambda: datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc))
    assert main(["--due", "--config", str(cfg_path)]) == 3


def test_failing_profile_is_reported_and_the_rest_still_run(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    import mailtriage.delivery as delivery_module
    from mailtriage.errors import MailError

    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    _pulls, sends = _stub_profile_pipeline(monkeypatch)

    def flaky_send(cfg: Config, kept: list[Triaged], stamp: str = "", events: Any = None) -> None:
        if cfg.delivery == "slack":
            raise MailError("SLACK_WEBHOOK_URL is not set.")
        sends.append((cfg.subject_prefix, cfg.delivery))

    monkeypatch.setattr(delivery_module, "send", flaky_send)

    assert main(["--config", str(_profiled(tmp_path))]) == 1
    assert sends == [("mt · home", "email")]
    err = capsys.readouterr().err
    assert "profile work: SLACK_WEBHOOK_URL is not set." in err
    assert "1 profile(s) failed: work" in err


def test_weekly_runs_per_profile_over_its_accounts(monkeypatch: Any, tmp_path: Any) -> None:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module

    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    pulls: list[Any] = []

    def fake_pull_week(environ: Any, now: Any, label: str, only: Any = None) -> WeekResult:
        pulls.append(only)
        return _week_result(acct={"replied": [_open_item("r", 1)], "archived": [], "open": []})

    monkeypatch.setattr(cli_module, "pull_week", fake_pull_week)
    sent: list[Any] = []
    monkeypatch.setattr(delivery_module, "send_html", lambda cfg, subject, html: sent.append(subject))

    assert main(["--weekly", "--config", str(_profiled(tmp_path))]) == 0
    assert pulls == [{"w@corp.com"}, {"h@gmail.com", "h2@gmail.com"}]
    assert sent == ["mt · work · weekly review", "mt · home · weekly review"]
