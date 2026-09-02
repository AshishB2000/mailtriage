"""pull_week is the weekly roll-up: everything carrying `label` in the last
`days` days, classified replied/archived/open by pure IMAP arithmetic, no
model call. Read-only throughout -- see the CRITICAL INVARIANT in
imap_pull's module docstring. No real network: a fake IMAP4_SSL stands in,
same pattern as tests/test_open_actions.py.
"""

from __future__ import annotations

import imaplib
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

from mailtriage.imap_pull import pull_week

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
LABEL = "mailtriage/action"
SENDER = "alice@gmail.com"
PW_VAR = "MAIL_PW_ALICE_GMAIL_COM"
ENV = {"MAIL_ACCOUNTS": SENDER, PW_VAR: "pw"}  # pull_week discovers accounts the same way pull() does


def _raw(subject: str, days_ago: float, message_id: str) -> bytes:
    dt = NOW - timedelta(days=days_ago)
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
    """Stand-in for imaplib.IMAP4_SSL, covering the All-mail label+SINCE
    search, the header-only FETCH, the Sent-mailbox reply check, and the
    INBOX presence check -- everything pull_week does. `select()` remembers
    the last-selected mailbox so `uid("SEARCH", ..., "X-GM-THRID", ...)` can
    tell the Sent-mailbox reply check apart from the INBOX presence check,
    same as pull_week itself distinguishes them by which mailbox is
    currently selected.
    """

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
        inbox_thrids: frozenset[str] = frozenset(),
        login_error: Exception | None = None,
    ) -> None:
        self.host, self.port = host, port
        self._label_uids = label_uids or []
        self._messages = messages or {}
        self._list_lines = (
            list_lines
            if list_lines is not None
            else [
                b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
                b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
            ]
        )
        self._replied_thrids = replied_thrids
        self._replied_message_ids = replied_message_ids
        self._inbox_thrids = inbox_thrids
        self._login_error = login_error
        self._selected: str | None = None
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
        self._selected = mailbox
        return "OK", [b"1"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.uid_calls.append((command, args))
        cmd = command.upper()
        if cmd == "SEARCH":
            crit = args[1]
            if crit == "X-GM-LABELS":
                return "OK", [b" ".join(self._label_uids)]
            if crit == "X-GM-THRID":
                thrid = args[2]
                pool = self._inbox_thrids if self._selected == "INBOX" else self._replied_thrids
                return "OK", [b"1" if thrid in pool else b""]
            if crit == "HEADER":
                mid = args[3].strip('"')
                return "OK", [b"1" if mid in self._replied_message_ids else b""]
            raise AssertionError(f"unexpected SEARCH criteria: {args}")
        if cmd == "FETCH":
            uid = args[0]
            items = args[1]
            raw, thrid = self._messages[uid]
            thrid_part = f"X-GM-THRID {thrid} " if thrid else ""
            line = f"1 (UID {uid.decode()} FLAGS () {thrid_part}{items} {{{len(raw)}}}".encode()
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


def test_all_mail_discovered_from_localized_list_line(monkeypatch: Any) -> None:
    factory = _patch_imap(
        monkeypatch,
        list_lines=[
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\All) "/" "[Gmail]/Alle Nachrichten"',
        ],
    )

    pull_week(ENV, NOW, LABEL)

    fake = factory.instances[0]
    assert fake.select_calls[0] == ('"[Gmail]/Alle Nachrichten"', True)


def test_since_date_computed_one_day_before_the_window(monkeypatch: Any) -> None:
    factory = _patch_imap(monkeypatch)

    pull_week(ENV, NOW, LABEL, days=7)

    fake = factory.instances[0]
    search_calls = [(cmd, args) for cmd, args in fake.uid_calls if cmd == "SEARCH"]
    label_search = next(args for cmd, args in search_calls if args[1] == "X-GM-LABELS")
    assert label_search == (None, "X-GM-LABELS", '"mailtriage/action"', "SINCE", "20-Aug-2026")


def test_classifies_replied_archived_and_open(monkeypatch: Any) -> None:
    _patch_imap(
        monkeypatch,
        label_uids=[b"101", b"102", b"103"],
        messages={
            b"101": (_raw("replied thread", 2, "<r@work.com>"), "111"),
            b"102": (_raw("archived thread", 2, "<a@work.com>"), "222"),
            b"103": (_raw("open thread", 2, "<o@work.com>"), "333"),
        },
        replied_thrids=frozenset({"111"}),
        inbox_thrids=frozenset({"333"}),  # 222 (archived) is absent from INBOX; 111 never checked
    )

    result = pull_week(ENV, NOW, LABEL)

    buckets = result["accounts"][SENDER]
    assert [it["subject"] for it in buckets["replied"]] == ["replied thread"]
    assert [it["subject"] for it in buckets["archived"]] == ["archived thread"]
    assert [it["subject"] for it in buckets["open"]] == ["open thread"]


def test_exact_window_filter_drops_mail_older_than_days(monkeypatch: Any) -> None:
    """SINCE is date-granular (server-side, opaque to this fake -- it just
    returns whatever label_uids the test hands it, standing in for whatever
    IMAP's day-level SINCE matched). pull_week must still drop anything
    older than the exact `days` cutoff in Python, same discipline as
    fetch_account's own hour-level re-filter."""
    _patch_imap(
        monkeypatch,
        label_uids=[b"301", b"302"],
        messages={
            b"301": (_raw("within the week", 6, "<w@work.com>"), ""),
            b"302": (_raw("just past the week", 7.5, "<p@work.com>"), ""),
        },
        inbox_thrids=frozenset(),
    )

    result = pull_week(ENV, NOW, LABEL, days=7)

    subjects = {it["subject"] for b in result["accounts"][SENDER].values() for it in b}
    assert subjects == {"within the week"}


def test_fetch_is_header_only_never_a_full_body(monkeypatch: Any) -> None:
    factory = _patch_imap(
        monkeypatch,
        label_uids=[b"401"],
        messages={b"401": (_raw("header only", 1, "<h@work.com>"), "")},
    )

    pull_week(ENV, NOW, LABEL)

    fetch_calls = [args for cmd, args in factory.instances[0].uid_calls if cmd == "FETCH"]
    assert len(fetch_calls) == 1
    items = fetch_calls[0][1]
    assert "HEADER.FIELDS" in items
    assert "BODY.PEEK[]" not in items, "pull_week must never fetch a full body, headers only"


def test_returns_full_week_item_shape(monkeypatch: Any) -> None:
    _patch_imap(
        monkeypatch,
        label_uids=[b"501"],
        messages={b"501": (_raw("full record", 3, "<full@work.com>"), "777")},
        inbox_thrids=frozenset(),  # not replied, and 777 absent from INBOX -> archived
    )

    result = pull_week(ENV, NOW, LABEL)

    item = result["accounts"][SENDER]["archived"][0]
    assert item["account"] == SENDER
    assert item["subject"] == "full record"
    assert item["sender"] == "alice@work.com"
    assert item["age_days"] == 3
    assert "full%40work.com" in item["link"]  # gmail_link quotes the message-id into the URL


def test_no_candidates_skips_sent_and_inbox_selects(monkeypatch: Any) -> None:
    factory = _patch_imap(monkeypatch, label_uids=[])

    result = pull_week(ENV, NOW, LABEL)

    assert result["accounts"][SENDER] == {"replied": [], "archived": [], "open": []}
    selected = [c[0] for c in factory.instances[0].select_calls]
    assert selected == ['"[Gmail]/All Mail"']  # never selects Sent or INBOX with nothing to check


def test_login_failure_is_a_warning_not_a_raise(monkeypatch: Any) -> None:
    _patch_imap(monkeypatch, login_error=OSError("login refused"))

    result = pull_week(ENV, NOW, LABEL)

    assert result["accounts"] == {}
    assert len(result["warnings"]) == 1
    assert "login refused" in result["warnings"][0]["error"]
