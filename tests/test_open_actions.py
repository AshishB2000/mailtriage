"""pull_open_actions re-surfaces needs_action mail label_actions labeled on a
prior run and that's still open. Read-only throughout, no model call -- see
the CRITICAL INVARIANT in imap_pull's module docstring. No real network: a
fake IMAP4_SSL stands in, same pattern as tests/test_push_drafts.py.
"""

from __future__ import annotations

import imaplib
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

from mailtriage.imap_pull import pull_open_actions

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
WINDOW_HOURS = 13
LABEL = "mailtriage/action"
SENDER = "alice@gmail.com"
PW_VAR = "MAIL_PW_ALICE_GMAIL_COM"
ENV = {"MAIL_ACCOUNTS": SENDER, PW_VAR: "pw"}  # pull_open_actions discovers accounts the same way pull() does


def _raw(subject: str, hours_ago: float, message_id: str) -> bytes:
    dt = NOW - timedelta(hours=hours_ago)
    return (
        f"From: alice@work.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {format_datetime(dt)}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"body\r\n"
    ).encode()


class _FakeIMAP:
    """Stand-in for imaplib.IMAP4_SSL, covering the label search, the
    labeled-message fetch (with X-GM-THRID), the Sent-mailbox discovery via
    LIST, and the reply search in Sent -- everything pull_open_actions does."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        label_uids: list[bytes] | None = None,
        messages: dict[bytes, tuple[bytes, str]] | None = None,  # uid -> (raw, thrid or "")
        list_lines: list[bytes] | None = None,
        replied_thrids: frozenset[str] = frozenset(),
        replied_message_ids: frozenset[str] = frozenset(),
        login_error: Exception | None = None,
    ) -> None:
        self.host, self.port = host, port
        self._label_uids = label_uids or []
        self._messages = messages or {}
        self._list_lines = (
            list_lines if list_lines is not None else [b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']
        )
        self._replied_thrids = replied_thrids
        self._replied_message_ids = replied_message_ids
        self._login_error = login_error
        self.login_calls: list[tuple[str, str]] = []
        self.select_calls: list[tuple[Any, ...]] = []
        self.uid_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.logged_out = False

    def login(self, user: str, pw: str) -> tuple[str, list[bytes]]:
        self.login_calls.append((user, pw))
        if self._login_error:
            raise self._login_error
        return "OK", [b"Logged in"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        return "OK", [b"1"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.uid_calls.append((command, args))
        cmd = command.upper()
        if cmd == "SEARCH":
            crit = args[1]
            if crit == "X-GM-LABELS":
                return "OK", [b" ".join(self._label_uids)]
            if crit == "X-GM-THRID":
                return "OK", [b"1" if args[2] in self._replied_thrids else b""]
            if crit == "HEADER":
                mid = args[3].strip('"')
                return "OK", [b"1" if mid in self._replied_message_ids else b""]
            raise AssertionError(f"unexpected SEARCH criteria: {args}")
        if cmd == "FETCH":
            uid = args[0]
            raw, thrid = self._messages[uid]
            thrid_part = f"X-GM-THRID {thrid} " if thrid else ""
            line = f"1 (UID {uid.decode()} FLAGS () {thrid_part}BODY[] {{{len(raw)}}}".encode()
            return "OK", [(line, raw), b")"]
        raise AssertionError(f"unexpected uid command: {command}")

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "OK", [b"Logging out"]

    # Defined last: naming a method `list` shadows the builtin for annotations
    # appearing after it in this class body, so every other method comes first.
    def list(self, *a: object, **k: object) -> tuple[str, list[bytes]]:
        return "OK", self._list_lines


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


def test_searches_inbox_by_label_readonly(monkeypatch: Any) -> None:
    factory = _patch_imap(monkeypatch)

    pull_open_actions(ENV, NOW, WINDOW_HOURS, LABEL)

    fake = factory.instances[0]
    assert fake.select_calls[0] == ("INBOX", True)
    search_calls = [(cmd, args) for cmd, args in fake.uid_calls if cmd == "SEARCH"]
    assert search_calls[0] == ("SEARCH", (None, "X-GM-LABELS", '"mailtriage/action"'))


def test_keeps_older_than_window_drops_in_window(monkeypatch: Any) -> None:
    _patch_imap(
        monkeypatch,
        label_uids=[b"101", b"102"],
        messages={
            b"101": (_raw("old one", 20, "<old@work.com>"), ""),  # older than the 13h window -- kept
            b"102": (_raw("recent one", 2, "<recent@work.com>"), ""),  # in-window -- normal pull() covers it
        },
    )

    result = pull_open_actions(ENV, NOW, WINDOW_HOURS, LABEL)

    assert [m["subject"] for m in result["messages"]] == ["old one"]


def test_drops_replied_thread_by_thrid(monkeypatch: Any) -> None:
    factory = _patch_imap(
        monkeypatch,
        label_uids=[b"201"],
        messages={b"201": (_raw("replied", 20, "<replied@work.com>"), "999")},
        replied_thrids=frozenset({"999"}),
    )

    result = pull_open_actions(ENV, NOW, WINDOW_HOURS, LABEL)

    assert result["messages"] == []
    search_calls = [(cmd, args) for cmd, args in factory.instances[0].uid_calls if cmd == "SEARCH"]
    assert ("SEARCH", (None, "X-GM-THRID", "999")) in search_calls


def test_keeps_unreplied_thread(monkeypatch: Any) -> None:
    _patch_imap(
        monkeypatch,
        label_uids=[b"301"],
        messages={b"301": (_raw("no reply yet", 20, "<noreply@work.com>"), "888")},
    )

    result = pull_open_actions(ENV, NOW, WINDOW_HOURS, LABEL)

    assert [m["subject"] for m in result["messages"]] == ["no reply yet"]


def test_falls_back_to_in_reply_to_header_when_thrid_missing(monkeypatch: Any) -> None:
    factory = _patch_imap(
        monkeypatch,
        label_uids=[b"401"],
        messages={b"401": (_raw("no thrid", 20, "<msg401@work.com>"), "")},  # X-GM-THRID unavailable
        replied_message_ids=frozenset({"<msg401@work.com>"}),
    )

    result = pull_open_actions(ENV, NOW, WINDOW_HOURS, LABEL)

    assert result["messages"] == []
    search_calls = [(cmd, args) for cmd, args in factory.instances[0].uid_calls if cmd == "SEARCH"]
    assert any(cmd == "SEARCH" and args[1] == "HEADER" for cmd, args in search_calls)


def test_sent_mailbox_discovered_from_localized_list_line(monkeypatch: Any) -> None:
    factory = _patch_imap(
        monkeypatch,
        label_uids=[b"501"],
        messages={b"501": (_raw("localized", 20, "<loc@work.com>"), "")},
        list_lines=[
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Verzonden berichten"',
        ],
    )

    pull_open_actions(ENV, NOW, WINDOW_HOURS, LABEL)

    sent_selects = [c for c in factory.instances[0].select_calls if c[0] != "INBOX"]
    assert sent_selects == [('"[Gmail]/Verzonden berichten"', True)]


def test_returns_full_email_record(monkeypatch: Any) -> None:
    _patch_imap(
        monkeypatch,
        label_uids=[b"601"],
        messages={b"601": (_raw("full record", 20, "<full@work.com>"), "")},
    )

    result = pull_open_actions(ENV, NOW, WINDOW_HOURS, LABEL)

    assert len(result["messages"]) == 1
    rec = result["messages"][0]
    assert rec["subject"] == "full record"
    assert rec["account"] == SENDER
    assert rec["message_id"] == "<full@work.com>"
    assert rec["body"] == "body"
    assert rec["uid"] == "601"


def test_login_failure_is_a_warning_not_a_raise(monkeypatch: Any) -> None:
    _patch_imap(monkeypatch, login_error=OSError("login refused"))

    result = pull_open_actions(ENV, NOW, WINDOW_HOURS, LABEL)

    assert result["messages"] == []
    assert len(result["warnings"]) == 1
    assert "login refused" in result["warnings"][0]["error"]
