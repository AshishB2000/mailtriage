"""label_actions applies the carry-over Gmail label to this run's needs_action
items so pull_open_actions can find them again next time. It must NEVER touch
a message body -- see the CRITICAL INVARIANT in imap_pull's module docstring.
No real network: a fake IMAP4_SSL stands in, same pattern as
tests/test_push_drafts.py.
"""

from __future__ import annotations

import imaplib
from typing import Any, cast

from mailtriage.imap_pull import label_actions
from mailtriage.models import Email, Triaged

SENDER = "alice@gmail.com"
PW_VAR = "MAIL_PW_F24FE3C393F64986"  # pw_env_var(SENDER): BLAKE2b-128 of the address, never the address
LABEL = "mailtriage/action"


def make_email(i: int, uid: str | None = None, **overrides: object) -> Email:
    base: Email = {
        "account": SENDER,
        "from": f"sender{i}@example.com",
        "subject": f"real subject {i}",
        "snippet": f"real snippet {i}",
        "body": f"real body {i}",
        "date": "2026-08-28T10:00:00+00:00",
        "unread": True,
        "link": f"https://real.example.com/{i}",
        "message_id": f"<real-{i}@example.com>",
        "reply_to": f"replyto{i}@example.com",
        "uid": str(100 + i) if uid is None else uid,
    }
    return cast(Email, {**base, **overrides})


def make_triaged(i: int, bucket: str = "needs_action", **overrides: object) -> Triaged:
    em = make_email(i)
    base: Triaged = {
        "bucket": bucket,
        "note": "reply",
        "account": em["account"],
        "sender": em["from"],
        "subject": em["subject"],
        "link": em["link"],
        "date": em["date"],
        "unread": em["unread"],
        "idx": i,
        "draft": "",
    }
    return cast(Triaged, {**base, **overrides})


class _FakeIMAP:
    """Stand-in for imaplib.IMAP4_SSL. Records every call so tests can assert
    on exactly what label_actions did -- including what it must never do."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        login_error: Exception | None = None,
        create_error: Exception | None = None,
    ) -> None:
        self.host, self.port = host, port
        self._login_error = login_error
        self._create_error = create_error
        self.login_calls: list[tuple[str, str]] = []
        self.select_calls: list[tuple[Any, ...]] = []
        self.create_calls: list[str] = []
        self.uid_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[Any] = []
        self.logged_out = False

    def login(self, user: str, pw: str) -> tuple[str, list[bytes]]:
        self.login_calls.append((user, pw))
        if self._login_error:
            raise self._login_error
        return "OK", [b"Logged in"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        return "OK", [b"1"]

    def create(self, name: str) -> tuple[str, list[bytes]]:
        self.create_calls.append(name)
        if self._create_error:
            raise self._create_error
        return "NO", [b"[ALREADYEXISTS] Label already exists"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.uid_calls.append((command, args))
        if command.upper() == "STORE":
            return "OK", [b"1 (X-GM-LABELS (...))"]
        raise AssertionError(f"unexpected uid command in label_actions: {command}")

    def fetch(self, *a: object, **k: object) -> tuple[str, list[bytes]]:  # pragma: no cover
        self.fetch_calls.append((a, k))
        raise AssertionError("label_actions must never FETCH a body")

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "OK", [b"Logging out"]


class _FakeIMAPFactory:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.instances: list[_FakeIMAP] = []

    def __call__(self, host: str, port: int) -> _FakeIMAP:
        inst = _FakeIMAP(host, port, **self.kwargs)  # type: ignore[arg-type]
        self.instances.append(inst)
        return inst


def _patch_imap(monkeypatch: Any, **kwargs: object) -> _FakeIMAPFactory:
    factory = _FakeIMAPFactory(**kwargs)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", factory)
    return factory


def test_label_actions_selects_inbox_read_write(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    factory = _patch_imap(monkeypatch)

    warnings = label_actions({PW_VAR: "pw"}, [make_triaged(0)], [make_email(0)], LABEL)

    assert warnings == []
    assert factory.instances[0].select_calls == [("INBOX", False)]


def test_label_actions_creates_label_and_ignores_alreadyexists(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    factory = _patch_imap(monkeypatch)

    label_actions({PW_VAR: "pw"}, [make_triaged(0)], [make_email(0)], LABEL)

    assert factory.instances[0].create_calls == ['"mailtriage/action"']


def test_label_actions_stores_quoted_label_by_uid_for_needs_action_only(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    factory = _patch_imap(monkeypatch)

    emails = [make_email(0, uid="501"), make_email(1, uid="502"), make_email(2, uid="503")]
    kept = [
        make_triaged(0, bucket="needs_action"),
        make_triaged(1, bucket="worth_reading"),  # not needs_action -- must not be labeled
        make_triaged(2, bucket="needs_action"),
    ]

    label_actions({PW_VAR: "pw"}, kept, emails, LABEL)

    store_calls = [(cmd, args) for cmd, args in factory.instances[0].uid_calls if cmd == "STORE"]
    assert store_calls == [
        ("STORE", ("501", "+X-GM-LABELS", '("mailtriage/action")')),
        ("STORE", ("503", "+X-GM-LABELS", '("mailtriage/action")')),
    ]


def test_label_actions_never_fetches_a_body(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    factory = _patch_imap(monkeypatch)

    label_actions({PW_VAR: "pw"}, [make_triaged(0)], [make_email(0)], LABEL)

    assert factory.instances[0].fetch_calls == []


def test_label_actions_login_failure_is_a_warning_not_a_raise(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    _patch_imap(monkeypatch, login_error=OSError("login refused"))

    warnings = label_actions({PW_VAR: "pw"}, [make_triaged(0)], [make_email(0)], LABEL)

    assert len(warnings) == 1
    assert warnings[0]["account"] == SENDER
    assert "login refused" in warnings[0]["error"]


def test_label_actions_missing_password_is_a_warning(monkeypatch: Any) -> None:
    _patch_imap(monkeypatch)

    warnings = label_actions({}, [make_triaged(0)], [make_email(0)], LABEL)

    assert len(warnings) == 1
    assert PW_VAR in warnings[0]["error"]


def test_label_actions_skips_when_no_needs_action_items(monkeypatch: Any) -> None:
    factory = _patch_imap(monkeypatch)

    kept = [make_triaged(0, bucket="worth_reading")]
    warnings = label_actions({}, kept, [make_email(0)], LABEL)

    assert warnings == []
    assert factory.instances == []  # never even connects when there's nothing to label


def test_label_actions_skips_item_with_no_uid(monkeypatch: Any) -> None:
    monkeypatch.setenv(PW_VAR, "pw")
    factory = _patch_imap(monkeypatch)

    emails = [make_email(0, uid="")]  # synthetic -- nothing to address
    warnings = label_actions({PW_VAR: "pw"}, [make_triaged(0)], emails, LABEL)

    assert warnings == []
    assert factory.instances == []
