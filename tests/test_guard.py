"""The no-double-send guard. catch_up_minutes lets two hourly cron firings land
inside one slot's window; imap_pull.already_delivered looks for the slot-stamped
subject in the user's own mailbox (Gmail is the memory -- no state file) and
cli.run sends nothing when it's there. Fake IMAP4_SSL, same pattern as
tests/test_open_actions.py. Manual runs carry no stamp and are never guarded.
"""

from __future__ import annotations

import imaplib
from datetime import datetime, timezone
from typing import Any

from mailtriage.cli import run
from mailtriage.config import Config
from mailtriage.imap_pull import already_delivered, check_login
from mailtriage.models import Email, Triaged

NOW = datetime(2026, 9, 3, 8, 20, tzinfo=timezone.utc)
SENDER = "alice@gmail.com"
PW_VAR = "MAIL_PW_ALICE_GMAIL_COM"
ENV = {"MAIL_ACCOUNTS": SENDER, PW_VAR: "pw"}
STAMP = "Thu 03 Sep 08:00"
STAMPED = f"mailtriage · {STAMP}"


def _raw(subject: str) -> bytes:
    return f"Subject: {subject}\r\n\r\n".encode()


class _FakeIMAP:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        subjects: dict[bytes, str] | None = None,  # uid -> Subject header, all returned by any SEARCH
        list_lines: list[bytes] | None = None,
        all_select_ok: bool = True,
        login_error: Exception | None = None,
        exists: int = 42,
    ) -> None:
        self._subjects = subjects or {}
        self._list_lines = list_lines if list_lines is not None else [b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']
        self._all_select_ok = all_select_ok
        self._login_error = login_error
        self._exists = exists
        self.select_calls: list[tuple[Any, ...]] = []
        self.uid_calls: list[tuple[str, tuple[Any, ...]]] = []

    def login(self, user: str, pw: str) -> tuple[str, list[bytes]]:
        if self._login_error:
            raise self._login_error
        return "OK", [b"Logged in"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        if "All Mail" in mailbox and not self._all_select_ok:
            return "NO", [b"Unknown Mailbox"]
        return "OK", [str(self._exists).encode()]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.uid_calls.append((command, args))
        if command == "SEARCH":
            return "OK", [b" ".join(self._subjects)]
        if command == "FETCH":
            raw = _raw(self._subjects[args[0]])
            return "OK", [(b"1 (BODY[HEADER.FIELDS (SUBJECT)] {n}", raw), b")"]
        raise AssertionError(f"unexpected uid command: {command}")

    def fetch(self, *a: object, **k: object) -> tuple[str, list[bytes]]:  # pragma: no cover
        raise AssertionError("the guard must never FETCH a body")

    def logout(self) -> tuple[str, list[bytes]]:
        return "OK", [b"bye"]

    def list(self, *a: object, **k: object) -> tuple[str, list[bytes]]:
        return "OK", self._list_lines


class _Factory:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.instances: list[_FakeIMAP] = []

    def __call__(self, host: str, port: int) -> _FakeIMAP:
        inst = _FakeIMAP(host, port, **self.kwargs)  # type: ignore[arg-type]
        self.instances.append(inst)
        return inst


def _patch_imap(monkeypatch: Any, **kwargs: object) -> _Factory:
    factory = _Factory(**kwargs)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", factory)
    return factory


# --- already_delivered ---------------------------------------------------


def test_found_in_all_mail(monkeypatch: Any) -> None:
    factory = _patch_imap(monkeypatch, subjects={b"7": STAMPED + " · 2 to act · 1 to read"})

    assert already_delivered(ENV, "mailtriage", STAMP, NOW) is True

    fake = factory.instances[0]
    assert fake.select_calls == [('"[Gmail]/All Mail"', True)]
    search = next(args for cmd, args in fake.uid_calls if cmd == "SEARCH")
    assert search == (None, "SUBJECT", '"Thu 03 Sep 08:00"', "SINCE", "02-Sep-2026")


def test_not_found_when_mailbox_is_empty(monkeypatch: Any) -> None:
    _patch_imap(monkeypatch)
    assert already_delivered(ENV, "mailtriage", STAMP, NOW) is False


def test_search_hit_with_a_different_prefix_is_not_a_match(monkeypatch: Any) -> None:
    """Gmail's SUBJECT search is fuzzy; a calendar invite at the same time
    must not suppress a real digest -- the full stamped prefix is verified."""
    _patch_imap(monkeypatch, subjects={b"7": "Invitation: standup Thu 03 Sep 08:00"})
    assert already_delivered(ENV, "mailtriage", STAMP, NOW) is False


def test_falls_back_to_inbox_and_sent_when_all_mail_is_missing(monkeypatch: Any) -> None:
    factory = _patch_imap(
        monkeypatch,
        all_select_ok=False,
        list_lines=[b'(\\Sent) "/" "[Gmail]/Sent Mail"'],
        subjects={b"1": STAMPED + " · 0 to act · 3 to read"},
    )
    assert already_delivered(ENV, "mailtriage", STAMP, NOW) is True
    boxes = [m for m, _ro in factory.instances[0].select_calls]
    assert boxes == ['"[Gmail]/All Mail"', '"INBOX"']  # found in INBOX; Sent never needed
    assert all(ro is True for _m, ro in factory.instances[0].select_calls)


def test_dead_account_never_vetoes_the_send(monkeypatch: Any) -> None:
    _patch_imap(monkeypatch, login_error=OSError("login refused"))
    assert already_delivered(ENV, "mailtriage", STAMP, NOW) is False


# --- check_login (doctor) ------------------------------------------------


def test_check_login_reports_inbox_count(monkeypatch: Any) -> None:
    factory = _patch_imap(monkeypatch, exists=17)
    assert check_login(ENV) == [(SENDER, 17, "")]
    assert factory.instances[0].select_calls == [("INBOX", True)]


def test_check_login_reports_failure_per_account(monkeypatch: Any) -> None:
    _patch_imap(monkeypatch, login_error=OSError("login refused"))
    addr, count, err = check_login(ENV)[0]
    assert (addr, count) == (SENDER, 0)
    assert "login refused" in err


# --- cli.run: stamped + guarded only when scheduled ----------------------


def _email() -> Email:
    return {
        "account": "acct",
        "from": "sender@example.com",
        "subject": "subject-0",
        "snippet": "s",
        "body": "b",
        "date": "2026-09-03T07:00:00+00:00",
        "unread": False,
        "link": "https://real.example.com/0",
        "message_id": "<msg-0@example.com>",
        "reply_to": "sender@example.com",
        "uid": "0",
    }


def _triaged() -> Triaged:
    return {
        "bucket": "worth_reading",
        "note": "fyi",
        "account": "acct",
        "sender": "sender@example.com",
        "subject": "subject-0",
        "link": "https://real.example.com/0",
        "date": "2026-09-03T07:00:00+00:00",
        "unread": False,
        "idx": 0,
        "draft": "",
    }


def _wire(monkeypatch: Any, delivered: bool | None) -> tuple[list[Any], list[Any]]:
    """Stub the pipeline; `delivered=None` makes the guard an assertion
    failure if it's ever consulted. Returns (guard_calls, sends)."""
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module

    guard_calls: list[Any] = []
    sends: list[Any] = []

    def fake_guard(environ: Any, prefix: str, stamp: str, now: datetime) -> bool:
        if delivered is None:
            raise AssertionError("a manual run must never consult the guard")
        guard_calls.append(f"{prefix} · {stamp}")
        return delivered

    monkeypatch.setattr(cli_module, "_now", lambda: NOW)
    monkeypatch.setattr(cli_module, "already_delivered", fake_guard)
    monkeypatch.setattr(
        cli_module, "pull", lambda environ, now, hours, only=None: {"messages": [_email()], "warnings": []}
    )
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [_triaged()])
    monkeypatch.setattr(delivery_module, "send", lambda cfg, kept, stamp="": sends.append(stamp))
    return guard_calls, sends


def _cfg() -> Config:
    return Config(
        delivery="email", email_to="me@example.com", email_from="bot@example.com", carry_over=False, run_at=["08:00"]
    )


def test_scheduled_run_is_stamped_and_sends_when_not_yet_delivered(monkeypatch: Any) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    guard_calls, sends = _wire(monkeypatch, delivered=False)

    run(_cfg(), dry_run=False)

    assert guard_calls == [STAMPED]
    assert sends == ["Thu 03 Sep 08:00"]


def test_scheduled_run_sends_nothing_when_already_delivered(monkeypatch: Any, capsys: Any) -> None:
    import mailtriage.cli as cli_module

    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    guard_calls, sends = _wire(monkeypatch, delivered=True)
    monkeypatch.setattr(cli_module, "pull", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no pull")))

    run(_cfg(), dry_run=False)

    assert guard_calls == [STAMPED]
    assert sends == []
    assert "already delivered — sending nothing" in capsys.readouterr().err


def test_manual_run_has_no_stamp_and_is_never_guarded(monkeypatch: Any) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    _guard_calls, sends = _wire(monkeypatch, delivered=None)

    run(_cfg(), dry_run=False)

    assert sends == [""]


def test_local_run_without_event_name_is_manual(monkeypatch: Any) -> None:
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    _guard_calls, sends = _wire(monkeypatch, delivered=None)

    run(_cfg(), dry_run=False)

    assert sends == [""]


def test_dry_run_is_never_guarded(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    _wire(monkeypatch, delivered=None)

    run(_cfg(), dry_run=True)

    assert "subject-0" in capsys.readouterr().out
