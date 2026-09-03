"""commands.py: Gmail labels and digest replies as the control plane. No real
network -- a fake IMAP4_SSL with a tiny in-memory label store stands in
(same pattern as tests/test_labels.py), shared across every connection a
run opens so cross-connection effects (handled-once, \\All writes) are real.
"""

from __future__ import annotations

import imaplib
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any

import pytest

from mailtriage import commands
from mailtriage.commands import (
    COMMAND_SCHEMA,
    LABEL_DONE,
    LABEL_HANDLED,
    LABEL_NEVER,
    LABEL_VIP,
    apply_label_commands,
    count_done,
    derive_sender_rules,
    handle_replies,
    item_map,
    parse_commands,
    snooze_days,
    until_date,
    until_label,
    user_text,
    with_sender_rules,
)
from mailtriage.config import Config

ME = "alice@gmail.com"
PW_VAR = "MAIL_PW_ALICE_GMAIL_COM"
ENV = {"MAIL_ACCOUNTS": ME, PW_VAR: "pw"}
ACTION = "mailtriage/action"
TODAY = date(2026, 9, 3)
NOW = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
CFG = Config(delivery="gmail", subject_prefix="mailtriage")


def _link(mid: str, account: str = ME) -> str:
    return f"https://mail.google.com/mail/u/{account}/#search/rfc822msgid:{mid.strip('<>').replace('@', '%40')}"


def _raw(subject: str, from_: str, mid: str, body: str = "body", html: str | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_
    msg["Subject"] = subject
    msg["Date"] = format_datetime(NOW)
    msg["Message-ID"] = mid
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


def _unq(s: str) -> str:
    return s[1:-1] if len(s) >= 2 and s[0] == s[-1] == '"' else s


class _FakeIMAP:
    """Tiny label store: uid -> {"labels": set, "raw": bytes}. Every
    connection the code opens shares one store (see _Factory), so a STORE on
    one connection is visible to a SEARCH on the next."""

    def __init__(self, host: str, port: int, *, store: dict[str, Any], login_error: Exception | None = None) -> None:
        self.store = store
        self._login_error = login_error
        self.select_calls: list[tuple[str, bool]] = []
        self.uid_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.create_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.logged_out = False

    def login(self, user: str, pw: str) -> tuple[str, list[bytes]]:
        if self._login_error:
            raise self._login_error
        return "OK", [b"OK"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        return "OK", [b"1"]

    def create(self, name: str) -> tuple[str, list[bytes]]:
        self.create_calls.append(_unq(name))
        self.store["mailboxes"].add(_unq(name))
        return "OK", [b"done"]

    def delete(self, name: str) -> tuple[str, list[bytes]]:
        self.delete_calls.append(_unq(name))
        self.store["mailboxes"].discard(_unq(name))
        return "OK", [b"done"]

    def _headers(self, uid: str) -> EmailMessage:
        from email import message_from_bytes, policy

        return message_from_bytes(self.store["msgs"][uid]["raw"], policy=policy.default)

    def _matches(self, uid: str, crit: list[str]) -> bool:
        labels = self.store["msgs"][uid]["labels"]
        i, ok = 0, True
        while i < len(crit):
            tok, neg = crit[i], False
            if tok == "NOT":
                neg, i = True, i + 1
                tok = crit[i]
            if tok == "X-GM-LABELS":
                hit, i = _unq(crit[i + 1]) in labels, i + 2
            elif tok == "SUBJECT":
                hit, i = _unq(crit[i + 1]).lower() in str(self._headers(uid).get("Subject", "")).lower(), i + 2
            elif tok == "SINCE":
                hit, i = True, i + 2
            elif tok == "HEADER":
                hit, i = _unq(crit[i + 2]) in str(self._headers(uid).get(crit[i + 1], "")), i + 3
            else:
                raise AssertionError(f"unexpected SEARCH token {tok!r} in {crit}")
            ok = ok and (hit != neg)
        return ok

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.uid_calls.append((command, args))
        cmd = command.upper()
        if cmd == "SEARCH":
            crit = list(args[1:])
            hits = [u for u in sorted(self.store["msgs"], key=int) if self._matches(u, crit)]
            return "OK", [" ".join(hits).encode()]
        if cmd == "FETCH":
            out: list[Any] = []
            for u in args[0].split(","):
                raw = self.store["msgs"][u]["raw"]
                out.append((f"1 (UID {u} FLAGS () BODY[] {{{len(raw)}}}".encode(), raw))
                out.append(b")")
            return "OK", out
        if cmd == "STORE":
            u, op, labels = args[0], args[1], args[2]
            label = _unq(labels.strip("()"))
            if op == "+X-GM-LABELS":
                self.store["msgs"][u]["labels"].add(label)
            elif op == "-X-GM-LABELS":
                self.store["msgs"][u]["labels"].discard(label)
            else:
                raise AssertionError(f"unexpected STORE op {op}")
            return "OK", [b"OK"]
        raise AssertionError(f"unexpected uid command {command}")

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "OK", [b"bye"]

    # Defined last: naming a method `list` shadows the builtin for annotations after it.
    def list(self, *a: object, **k: object) -> tuple[str, list[bytes]]:
        lines = [b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"', b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"']
        lines += [f'(\\HasNoChildren) "/" "{m}"'.encode() for m in sorted(self.store["mailboxes"])]
        return "OK", lines


class _Factory:
    def __init__(self, msgs: dict[str, tuple[bytes, set[str]]], mailboxes: set[str] | None = None, **kw: Any) -> None:
        self.store: dict[str, Any] = {
            "msgs": {u: {"raw": raw, "labels": set(labels)} for u, (raw, labels) in msgs.items()},
            "mailboxes": set(mailboxes or set()),
        }
        self.kw = kw
        self.instances: list[_FakeIMAP] = []

    def __call__(self, host: str, port: int) -> _FakeIMAP:
        inst = _FakeIMAP(host, port, store=self.store, **self.kw)
        self.instances.append(inst)
        return inst

    def labels(self, uid: str) -> set[str]:
        return set(self.store["msgs"][uid]["labels"])


def _patch(
    monkeypatch: Any, msgs: dict[str, tuple[bytes, set[str]]], mailboxes: set[str] | None = None, **kw: Any
) -> _Factory:
    f = _Factory(msgs, mailboxes, **kw)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", f)
    return f


# --- pure parsing --------------------------------------------------------


@pytest.mark.parametrize(
    "label, days",
    [
        ("mailtriage/snooze-1d", 1),
        ("mailtriage/snooze-3d", 3),
        ("mailtriage/snooze-90d", 90),
        ("mailtriage/snooze-1w", 7),
        ("mailtriage/snooze-2w", 14),
        ("mailtriage/snooze-0d", None),
        ("mailtriage/snooze-91d", None),
        ("mailtriage/snooze-13w", None),
        ("mailtriage/snooze-", None),
        ("mailtriage/done", None),
        ("other/snooze-3d", None),
    ],
)
def test_snooze_days(label: str, days: int | None) -> None:
    assert snooze_days(label) == days


def test_until_round_trip_and_garbage() -> None:
    assert until_label(TODAY) == "mailtriage/until-2026-09-03"
    assert until_date("mailtriage/until-2026-09-03") == TODAY
    assert until_date("mailtriage/until-soon") is None
    assert until_date("mailtriage/done") is None


def test_item_map_reads_html_anchor_form() -> None:
    html = f'<a href="{_link("<a@x.com>")}" style="x">#1</a> <a href="{_link("<a@x.com>")}">Subject</a>' + (
        f'<a href="{_link("<b@y.com>", "bob@gmail.com")}">#2</a>'
    )
    assert item_map(html, "") == {1: (ME, "a@x.com"), 2: ("bob@gmail.com", "b@y.com")}


def test_item_map_falls_back_to_plain_text_form() -> None:
    text = f"> #1 <{_link('<a@x.com>')}> Subject\n> #2 <{_link('<b@y.com>')}> Other"
    assert item_map("", text) == {1: (ME, "a@x.com"), 2: (ME, "b@y.com")}


def test_item_map_ignores_a_number_the_reader_typed() -> None:
    # "#3" in the reader's own words is not adjacent to any link -> never pairs.
    html = f'<div>done #3</div><blockquote><a href="{_link("<a@x.com>")}">#1</a></blockquote>'
    assert item_map(html, "") == {1: (ME, "a@x.com")}
    text = f"done #3\n\n> #1 <{_link('<a@x.com>')}>"
    assert item_map("", text) == {1: (ME, "a@x.com")}


def test_user_text_stops_at_gmail_attribution_even_when_wrapped() -> None:
    body = (
        "done 1 and snooze 2\n\nOn Wed, Sep 2, 2026 at 6:00 PM alice\n<alice@gmail.com> wrote:\n> mailtriage\n> #1 ..."
    )
    assert user_text(body) == "done 1 and snooze 2"
    assert user_text("never 4\n> quoted") == "never 4"
    assert user_text("On my way -- draft 1 shorter\n\n> quoted") == "On my way -- draft 1 shorter"


def test_parse_commands_is_hostile_input_safe() -> None:
    items = {1: (ME, "a"), 2: (ME, "b")}
    reply = {
        "commands": [
            {"action": "done", "item": 1, "days": 0, "instruction": ""},
            {"action": "done", "item": 1, "days": 0, "instruction": ""},  # duplicate
            {"action": "snooze", "item": 2, "days": 400, "instruction": ""},  # bad days -> default
            {"action": "snooze", "item": True, "days": 3, "instruction": ""},  # bool item
            {"action": "delete", "item": 2, "days": 0, "instruction": ""},  # unknown action
            {"action": "draft", "item": 9, "days": 0, "instruction": "x"},  # unknown item
            {"action": "draft", "item": 2, "days": 0, "instruction": None},
            "garbage",
        ]
    }
    got = parse_commands(reply, items)
    assert got == [
        {"action": "done", "item": 1, "days": 7, "instruction": ""},
        {"action": "snooze", "item": 2, "days": 7, "instruction": ""},
        {"action": "draft", "item": 2, "days": 7, "instruction": ""},
    ]
    assert parse_commands({"commands": "nope"}, items) == []
    assert parse_commands({}, items) == []


# --- label commands ------------------------------------------------------


def _msgs(*specs: tuple[str, str, set[str]]) -> dict[str, tuple[bytes, set[str]]]:
    return {uid: (_raw(f"subj {uid}", from_, f"<m{uid}@x.com>"), labels) for uid, from_, labels in specs}


def test_done_removes_action_and_skips_from_candidates(monkeypatch: Any) -> None:
    f = _patch(monkeypatch, _msgs(("1", "a@x.com", {ACTION, LABEL_DONE}), ("2", "b@x.com", {ACTION})))

    out = apply_label_commands(ENV, TODAY, ACTION)

    assert out["warnings"] == []
    assert out["counts"] == {"done": 1, "snoozed": 0, "woken": 0}
    assert f.labels("1") == {LABEL_DONE}  # action gone, done kept as the record
    assert f.labels("2") == {ACTION}
    assert out["skip"] == {ME: {"1"}}
    assert f.instances[0].select_calls == [("INBOX", False)]  # read-write, same as label_actions
    assert f.instances[0].logged_out

    # Idempotent: a second run changes nothing and counts nothing.
    again = apply_label_commands(ENV, TODAY, ACTION)
    assert again["counts"]["done"] == 0 and again["skip"] == {ME: {"1"}}


def test_default_labels_created_on_every_run(monkeypatch: Any) -> None:
    f = _patch(monkeypatch, {})
    apply_label_commands(ENV, TODAY, ACTION)
    assert set(f.instances[0].create_calls) >= {LABEL_DONE, LABEL_NEVER, LABEL_VIP, "mailtriage/snooze-1w"}


def test_snooze_becomes_dated_until_label(monkeypatch: Any) -> None:
    f = _patch(monkeypatch, _msgs(("1", "a@x.com", {ACTION, "mailtriage/snooze-3d"})), {"mailtriage/snooze-3d"})

    out = apply_label_commands(ENV, TODAY, ACTION)

    assert out["counts"]["snoozed"] == 1
    assert f.labels("1") == {"mailtriage/until-2026-09-06"}
    assert "mailtriage/until-2026-09-06" in f.instances[0].create_calls
    assert out["skip"] == {ME: {"1"}}


def test_until_wakes_when_due_and_label_mailbox_is_deleted(monkeypatch: Any) -> None:
    expired, future = "mailtriage/until-2026-09-03", "mailtriage/until-2026-09-10"
    f = _patch(
        monkeypatch,
        _msgs(("1", "a@x.com", {expired}), ("2", "b@x.com", {future, ACTION})),
        {expired, future, "mailtriage/until-2026-08-01"},  # the last one is already empty
    )

    out = apply_label_commands(ENV, TODAY, ACTION)

    assert out["counts"]["woken"] == 1
    assert f.labels("1") == {ACTION}  # back in carry-over
    assert f.labels("2") == {future}  # still asleep: action stripped so carry-over can't see it
    assert out["skip"] == {ME: {"2"}}
    assert set(f.instances[0].delete_calls) == {expired, "mailtriage/until-2026-08-01"}
    assert future in f.store["mailboxes"]


def test_missing_labels_are_not_an_error(monkeypatch: Any) -> None:
    f = _patch(monkeypatch, _msgs(("1", "a@x.com", {ACTION})))
    out = apply_label_commands(ENV, TODAY, ACTION)
    assert out["warnings"] == [] and out["counts"] == {"done": 0, "snoozed": 0, "woken": 0}
    assert f.labels("1") == {ACTION}


def test_login_failure_and_missing_accounts_are_warnings(monkeypatch: Any) -> None:
    _patch(monkeypatch, {}, login_error=OSError("refused"))
    out = apply_label_commands(ENV, TODAY, ACTION)
    assert len(out["warnings"]) == 1 and "refused" in out["warnings"][0]["error"]

    out = apply_label_commands({}, TODAY, ACTION)  # no MAIL_ACCOUNTS: warn, never raise
    assert out["counts"]["done"] == 0 and "MAIL_ACCOUNTS" in out["warnings"][0]["error"]


# --- never / vip sender derivation --------------------------------------


def test_derive_sender_rules_reads_from_headers_in_all_mail_readonly(monkeypatch: Any) -> None:
    f = _patch(
        monkeypatch,
        _msgs(
            ("1", '"Spammy" <Newsletter@Corp.com>', {LABEL_NEVER}),
            ("2", "boss@corp.com", {LABEL_VIP}),
            ("3", "nobody@corp.com", {ACTION}),
        ),
    )

    out = derive_sender_rules(ENV)

    assert out["never"] == {"newsletter@corp.com"}
    assert out["vip"] == {"boss@corp.com"}
    fake = f.instances[0]
    assert fake.select_calls == [('"[Gmail]/All Mail"', True)]
    fetches = [a for c, a in fake.uid_calls if c == "FETCH"]
    assert all("HEADER.FIELDS (FROM)" in a[1] for a in fetches)  # header-only, never a body

    cfg = with_sender_rules(Config(delivery="gmail", rules={"always_ignore": ["x@y.com"]}), out["never"], out["vip"])
    assert cfg.rules["always_ignore"] == ["x@y.com", "newsletter@corp.com"]
    assert cfg.rules["always_surface"] == ["boss@corp.com"]
    assert with_sender_rules(cfg, set(), set()) is cfg


def test_count_done_searches_all_mail(monkeypatch: Any) -> None:
    f = _patch(monkeypatch, _msgs(("1", "a@x.com", {LABEL_DONE}), ("2", "b@x.com", {LABEL_DONE}), ("3", "c", {ACTION})))
    out = count_done(ENV, NOW)
    assert out["done"] == 2
    assert f.instances[0].select_calls == [('"[Gmail]/All Mail"', True)]
    assert not [c for c, _ in f.instances[0].uid_calls if c == "FETCH"]


# --- replies to the digest -----------------------------------------------


def _reply(text: str, items: dict[int, str], from_: str = ME, subject: str = "Re: mailtriage · 2 to act") -> bytes:
    quoted_html = "".join(
        f'<a href="{_link(mid)}">#{n}</a> <a href="{_link(mid)}">subj</a>' for n, mid in items.items()
    )
    html = f"<div>{text}</div><blockquote>{quoted_html}</blockquote>"
    plain = f"{text}\n\nOn Wed, Sep 2, 2026 alice wrote:\n> mailtriage\n"
    return _raw(subject, from_, "<reply1@gmail.com>", plain, html)


def _run(monkeypatch: Any, f: _Factory, model_reply: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    def fake_call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        assert schema == COMMAND_SCHEMA
        calls.append((system, user))
        return model_reply

    return handle_replies(CFG, ENV, NOW, TODAY, lambda: fake_call), calls


def test_reply_done_is_applied_in_all_mail_and_handled_exactly_once(monkeypatch: Any) -> None:
    msgs = _msgs(("1", "a@x.com", {ACTION}), ("2", "b@x.com", {ACTION}))
    msgs["9"] = (_reply("done 2", {1: "<m1@x.com>", 2: "<m2@x.com>"}), set())
    f = _patch(monkeypatch, msgs)

    out, calls = _run(monkeypatch, f, {"commands": [{"action": "done", "item": 2, "days": 0, "instruction": ""}]})

    assert out["warnings"] == []
    assert out["replies"] == 1 and out["counts"]["done"] == 1
    assert out["skip_message_ids"] == {"<reply1@gmail.com>"}
    assert len(calls) == 1
    assert "Items in the digest: #1, #2" in calls[0][1] and "done 2" in calls[0][1]
    assert f.labels("2") == {LABEL_DONE} and f.labels("1") == {ACTION}
    assert f.labels("9") == {LABEL_HANDLED}
    # Discovery in INBOX read-write (handled label STOREd there); commands in \All read-write.
    assert f.instances[0].select_calls == [("INBOX", False)]
    assert f.instances[1].select_calls == [('"[Gmail]/All Mail"', False)]
    assert ("SEARCH", (None, "HEADER", "Message-ID", '"m2@x.com"')) in f.instances[1].uid_calls

    # Second run: the handled label hides the reply -- no model call, no change.
    out2, calls2 = _run(monkeypatch, f, {"commands": []})
    assert out2["replies"] == 0 and calls2 == []


def test_reply_snooze_never_vip_and_draft(monkeypatch: Any) -> None:
    msgs = _msgs(
        ("1", "a@x.com", {ACTION}), ("2", "b@x.com", {ACTION}), ("3", "c@x.com", set()), ("4", "d@x.com", {ACTION})
    )
    msgs["9"] = (
        _reply("snooze 1 for a week, never 2, vip 3, draft 4 shorter", {n: f"<m{n}@x.com>" for n in (1, 2, 3, 4)}),
        set(),
    )
    f = _patch(monkeypatch, msgs)
    pushed: list[Any] = []

    def fake_push(environ: Any, triaged: Any, emails: Any) -> list[dict[str, str]]:
        pushed.append((triaged, emails))
        return []

    monkeypatch.setattr(commands, "push_drafts", fake_push)
    seen_style: dict[str, Any] = {}

    def fake_call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        if schema == COMMAND_SCHEMA:
            return {
                "commands": [
                    {"action": "snooze", "item": 1, "days": 7, "instruction": ""},
                    {"action": "never", "item": 2, "days": 0, "instruction": ""},
                    {"action": "vip", "item": 3, "days": 0, "instruction": ""},
                    {"action": "draft", "item": 4, "days": 0, "instruction": "shorter"},
                ]
            }
        seen_style["system"] = system  # the draft call
        return {"items": [{"id": 0, "draft": "Short reply."}]}

    out = handle_replies(CFG, ENV, NOW, TODAY, lambda: fake_call)

    assert out["warnings"] == []
    assert out["counts"] == {"done": 0, "snooze": 1, "draft": 1, "never": 1, "vip": 1, "skipped": 0}
    assert f.labels("1") == {"mailtriage/until-2026-09-10"}
    assert f.labels("2") == {ACTION, LABEL_NEVER}
    assert f.labels("3") == {LABEL_VIP}
    assert "The reader asked for this draft specifically: shorter" in seen_style["system"]
    assert len(pushed) == 1 and pushed[0][0][0]["draft"] == "Short reply."
    assert pushed[0][1][0]["subject"] == "subj 4"


def test_reply_from_stranger_or_without_re_is_ignored(monkeypatch: Any) -> None:
    msgs = _msgs(("1", "a@x.com", {ACTION}))
    msgs["8"] = (_reply("done 1", {1: "<m1@x.com>"}, from_="stranger@evil.com"), set())
    msgs["9"] = (_reply("done 1", {1: "<m1@x.com>"}, subject="mailtriage · 1 to act"), set())  # the digest itself
    f = _patch(monkeypatch, msgs)

    out, calls = _run(monkeypatch, f, {"commands": [{"action": "done", "item": 1, "days": 0, "instruction": ""}]})

    assert out["replies"] == 0 and calls == []
    assert f.labels("1") == {ACTION}
    assert f.labels("8") == set() and f.labels("9") == set()


def test_reply_with_hostile_model_output_is_still_marked_handled(monkeypatch: Any) -> None:
    msgs = _msgs(("1", "a@x.com", {ACTION}))
    msgs["9"] = (_reply("done 1", {1: "<m1@x.com>"}), set())
    f = _patch(monkeypatch, msgs)

    out, _calls = _run(monkeypatch, f, {"commands": [{"action": "done", "item": 7, "days": 0, "instruction": ""}]})

    assert out["replies"] == 1 and out["counts"]["done"] == 0
    assert f.labels("1") == {ACTION}
    assert f.labels("9") == {LABEL_HANDLED}


def test_reply_target_no_longer_findable_is_counted_skipped(monkeypatch: Any) -> None:
    msgs = _msgs(("1", "a@x.com", {ACTION}))
    msgs["9"] = (_reply("done 1", {1: "<gone@x.com>"}), set())
    f = _patch(monkeypatch, msgs)

    out, _calls = _run(monkeypatch, f, {"commands": [{"action": "done", "item": 1, "days": 0, "instruction": ""}]})

    assert out["counts"]["skipped"] == 1 and out["counts"]["done"] == 0
    assert f.labels("9") == {LABEL_HANDLED}
