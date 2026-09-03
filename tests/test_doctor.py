"""`mailtriage --doctor`: one PASS/FAIL line per check, exit 1 if any failed.
Every check is stubbed at its seam -- no network, no real send."""

from __future__ import annotations

from typing import Any

import yaml

from mailtriage.cli import main
from mailtriage.errors import MailError
from mailtriage.models import Email


def _config(tmp_path: Any) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"delivery": "email", "email_to": "me@example.com", "email_from": "b@example.com"}))
    return str(p)


def _wire(
    monkeypatch: Any, *, accounts: Any = None, kept_bucket: str | None = "needs_action", send_error: str = ""
) -> list[Any]:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    sent: list[Any] = []
    monkeypatch.setattr(
        cli_module,
        "check_login",
        lambda environ: accounts or [("alice@gmail.com", 12, "", "gmail mode · keywords=yes")],
    )
    monkeypatch.setattr(triage_module, "select_backend", lambda cfg, environ: ("stub", None))

    def fake_triage(cfg: Any, emails: list[Email], now: Any) -> list[Any]:
        assert len(emails) == 3 and "signed contract" in emails[0]["snippet"]
        if kept_bucket is None:
            return []
        return [{"bucket": kept_bucket, "subject": emails[0]["subject"]}]

    monkeypatch.setattr(triage_module, "triage", fake_triage)

    def fake_send_html(cfg: Any, subject: str, html: str) -> None:
        if send_error:
            raise MailError(send_error)
        sent.append(subject)

    monkeypatch.setattr(delivery_module, "send_html", fake_send_html)
    return sent


def test_all_pass_exits_0(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    sent = _wire(monkeypatch)
    assert main(["--doctor", "--config", _config(tmp_path)]) == 0
    err = capsys.readouterr().err
    assert "PASS config" in err
    assert "PASS account alice@gmail.com — ok: 12 in INBOX · gmail mode" in err
    assert "PASS provider stub" in err
    assert "PASS delivery email" in err
    assert "FAIL" not in err
    assert sent == ["mailtriage · doctor"]


def test_bad_config_fails_fast(tmp_path: Any, capsys: Any) -> None:
    assert main(["--doctor", "--config", str(tmp_path / "nope.yaml")]) == 1
    err = capsys.readouterr().err
    assert "FAIL config" in err and "not found" in err


def test_account_failure_names_the_secret_and_exits_1(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    _wire(monkeypatch, accounts=[("alice@gmail.com", 0, "IMAP4.error: AUTHENTICATIONFAILED", "")])
    assert main(["--doctor", "--config", _config(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "FAIL account alice@gmail.com" in err and "MAIL_PW_" in err
    assert "PASS provider" in err  # later checks still run


def test_provider_that_returns_no_needs_action_fails(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    _wire(monkeypatch, kept_bucket="worth_reading")
    assert main(["--doctor", "--config", _config(tmp_path)]) == 1
    assert "FAIL provider stub" in capsys.readouterr().err


def test_provider_error_is_a_fail_line_not_a_crash(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    import mailtriage.triage as triage_module

    _wire(monkeypatch)
    monkeypatch.setattr(
        triage_module,
        "select_backend",
        lambda cfg, environ: (_ for _ in ()).throw(MailError("No AI provider configured")),
    )
    assert main(["--doctor", "--config", _config(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "FAIL provider — No AI provider configured" in err
    assert "Traceback" not in err


def test_delivery_error_is_a_fail_line(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    _wire(monkeypatch, send_error="RESEND_API_KEY is not set.")
    assert main(["--doctor", "--config", _config(tmp_path)]) == 1
    assert "FAIL delivery email — RESEND_API_KEY is not set." in capsys.readouterr().err


def test_missing_mail_accounts_is_a_fail_line(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module

    _wire(monkeypatch)
    monkeypatch.setattr(
        cli_module, "check_login", lambda environ: (_ for _ in ()).throw(MailError("MAIL_ACCOUNTS is empty"))
    )
    assert main(["--doctor", "--config", _config(tmp_path)]) == 1
    assert "FAIL accounts — MAIL_ACCOUNTS is empty" in capsys.readouterr().err
