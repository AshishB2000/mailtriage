"""The offline pre-flight check that runs before any API spend.

This is deliberately *not* pytest: it must run on a fork's Actions runner
where `anthropic` may not even be importable yet, catch a logic regression
before the model is ever called, and need no test framework to do it.
`tests/` covers the same functions in more depth; this is the fast gate that
ships with the workflow.

HARD CONSTRAINT: this module must import ONLY pure functions. `triage.pick`
and `triage.select_backend` are safe because `anthropic` is imported lazily
inside `triage.claude_api.call`, not at module scope — importing them here
must never drag `anthropic` in.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mailtriage.config import Config
from mailtriage.drafts import DRAFT_SCHEMA, generate_drafts
from mailtriage.errors import MailError
from mailtriage.imap_pull import _older_than_window, _quote_mailbox, parse_message, within_window
from mailtriage.models import Email, Triaged
from mailtriage.rules import enforce, matches
from mailtriage.schedule import due, max_gap_hours
from mailtriage.triage import pick, select_backend


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


def self_check() -> None:
    # 1. Parser + date landmine: parsedate_to_datetime must stay tz-aware, or
    # within_window's UTC arithmetic silently shifts the window by hours.
    raw = (
        b"From: sender@example.com\r\n"
        b"To: me@gmail.com\r\n"
        b"Subject: hi\r\n"
        b"Date: Fri, 28 Aug 2026 09:00:00 +0000\r\n"
        b"Message-ID: <abc123@example.com>\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"hello world\r\n"
    )
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    rec = parse_message(raw, "me@gmail.com", "1 (FLAGS () BODY[]", now, 13)
    assert rec is not None, "an in-window message with a valid Date header must not be dropped"
    assert rec["subject"] == "hi", "parse_message must copy Subject through unchanged"
    assert rec["date"] == "2026-08-28T09:00:00+00:00", (
        "date landmine — parsedate_to_datetime must stay tz-aware, or the digest window silently shifts by hours"
    )

    # 2. Window drops: undated must be excluded, future must clamp, small skew allowed.
    assert within_window(None, now, 13) is False, "undated must drop, or an undated feed floods every run"
    assert within_window(now + timedelta(hours=1), now, 13) is False, (
        "future must clamp, or a sender with a fast clock stays permanently 'recent'"
    )
    assert within_window(now + timedelta(seconds=60), now, 13) is True, (
        "5-minute skew must be allowed, or borderline messages vanish at the window edge"
    )

    # 2b. Carried-mail window boundary: pull_open_actions must keep only mail
    # older than the window (an in-window hit is already covered by the
    # normal dated fetch, so counting it here would duplicate the digest).
    assert _older_than_window(now - timedelta(hours=20), now, 13) is True, (
        "older-than-window mail must be kept, or nothing ever carries over"
    )
    assert _older_than_window(now - timedelta(hours=2), now, 13) is False, (
        "in-window mail must be dropped here, or it would duplicate the normal fetch path"
    )
    assert _older_than_window(None, now, 13) is False, "undated must drop, same as everywhere else"

    # 2c. Label quoting: imaplib does not auto-quote, so the STORE/SEARCH
    # value for a Gmail label must escape '"' and '\' itself or a label
    # containing either breaks the IMAP command line.
    assert _quote_mailbox('a"b\\c') == '"a\\"b\\\\c"', (
        "label quoting must escape both '\"' and '\\\\', or a label containing either breaks IMAP"
    )

    # 3. pick() is the security layer: every hostile-model case must be handled
    # here, without a network round trip.
    emails = [_email(i) for i in range(14)]
    cfg = Config(delivery="email", reading_count=8)
    items = [
        {"id": 0, "bucket": "needs_action", "note": "reply to boss"},
        {"id": 1, "bucket": "needs_action", "note": "pay invoice"},
        # hostile: model tries to overwrite the real link/subject for id 2.
        {"id": 2, "bucket": "needs_action", "note": "rsvp", "link": "http://evil.example/", "subject": "EVIL"},
        {"id": 13, "bucket": "noise", "note": "unknown bucket, must be dropped"},
        # "carried" is client-authored only (imap_pull.pull_open_actions) --
        # the model's own bucket enum never grew it, so pick() must still
        # reject it exactly like any other unknown bucket.
        {"id": 12, "bucket": "carried", "note": "client-only bucket, must be dropped"},
        {"id": 99, "bucket": "worth_reading", "note": "out of range, must be dropped"},
        {"id": True, "bucket": "worth_reading", "note": "bool id, must be dropped"},
        *({"id": i, "bucket": "worth_reading", "note": f"note-{i}"} for i in range(3, 13)),  # 10 candidates
        {"id": 3, "bucket": "worth_reading", "note": "duplicate of above, must be dropped"},
    ]
    picked = pick(cfg, emails, {"items": items})
    needs_action = [p for p in picked if p["bucket"] == "needs_action"]
    worth_reading = [p for p in picked if p["bucket"] == "worth_reading"]

    assert not any("must be dropped" in p["note"] for p in picked), (
        "an unknown bucket, an out-of-range id, or a bool id was accepted instead of dropped"
    )
    assert len(needs_action) == 3, "needs_action must keep every item — it has no cap"
    assert len(worth_reading) == 8, (
        f"worth_reading must be capped at cfg.reading_count ({cfg.reading_count}), or padding kills the digest"
    )
    assert len({p["subject"] for p in worth_reading}) == 8, (
        "a duplicate id in the model reply must be deduped, not counted twice"
    )
    injected = next(p for p in needs_action if p["note"] == "rsvp")
    assert injected["link"] == emails[2]["link"] and injected["subject"] == emails[2]["subject"], (
        "link/subject must always come from the real Email, never from model-supplied fields in the reply"
    )

    # 4. Provider auto-order: PROVIDERS dict order decides the winner when
    # multiple secrets are set. claude-subscription must win here, or a fork
    # that has both a Claude subscription and an OpenAI key silently switches
    # providers the moment someone adds the second secret.
    auto_cfg = Config(delivery="email", provider="auto")
    fake_environ = {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "OPENAI_API_KEY": "key"}
    name, _call = select_backend(auto_cfg, fake_environ)
    assert name == "claude-subscription", (
        "auto-order regression -- a later PROVIDERS entry must never win over an earlier one"
    )

    # 5. An unknown 'provider' in config.yaml must raise, not silently pass through.
    try:
        Config.from_mapping({"delivery": "email", "provider": "bogus"})
    except MailError:
        pass
    else:
        raise AssertionError("an unknown 'provider' in config.yaml must raise MailError")

    # 6. DRAFT_SCHEMA must be strict everywhere: every object node forbids
    # additionalProperties and requires every property it declares.
    def _assert_strict_schema(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, (
                    f"object node missing additionalProperties:false: {node}"
                )
                props = node.get("properties", {})
                assert set(node.get("required", [])) == set(props), (
                    f"object node's 'required' must cover every property, or an ill-formed reply parses anyway: {node}"
                )
            for v in node.values():
                _assert_strict_schema(v)
        elif isinstance(node, list):
            for v in node:
                _assert_strict_schema(v)

    _assert_strict_schema(DRAFT_SCHEMA)

    # 7. generate_drafts is the security layer for drafts, same discipline as
    # pick(): a bool id, an out-of-range id, and a duplicate id must all be
    # dropped, leaving only the one legitimate draft attached.
    def _triaged_needs_action(i: int) -> Triaged:
        em = _email(i)
        return {
            "bucket": "needs_action",
            "note": f"note-{i}",
            "account": em["account"],
            "sender": em["from"],
            "subject": em["subject"],
            "link": em["link"],
            "date": em["date"],
            "unread": em["unread"],
            "idx": i,
            "draft": "",
        }

    draft_emails = [_email(i) for i in range(2)]
    draft_triaged = [_triaged_needs_action(0), _triaged_needs_action(1)]

    def hostile_call(_cfg: Config, _system: str, _user: str, _schema: dict) -> dict:  # type: ignore[type-arg]
        return {
            "items": [
                {"id": True, "draft": "bool id, must be dropped"},
                {"id": 99, "draft": "out of range, must be dropped"},
                {"id": 0, "draft": "the one legitimate draft"},
                {"id": 0, "draft": "duplicate of above, must be dropped"},
            ]
        }

    generate_drafts(cfg, hostile_call, draft_emails, draft_triaged)
    assert draft_triaged[0]["draft"] == "the one legitimate draft", (
        "generate_drafts must attach the single valid draft and drop every hostile id"
    )
    assert draft_triaged[1]["draft"] == "", "an id the model never drafted for must stay empty, not be invented"

    # 8. generate_drafts must make zero calls when nothing needs action.
    def _boom(_cfg: Config, _system: str, _user: str, _schema: dict) -> dict:  # type: ignore[type-arg]
        raise AssertionError("generate_drafts must not call the model when nothing needs action")

    no_action: list[Triaged] = [{**_triaged_needs_action(0), "bucket": "worth_reading"}]
    generate_drafts(cfg, _boom, draft_emails, no_action)

    # 9. rules.matches: domain rules match subdomains, never a mere suffix, and
    # a display-name From header must not defeat the address parse.
    assert matches("@corp.com", "x@mail.corp.com") is True, "a domain rule must match its subdomains"
    assert matches("@corp.com", "x@notcorp.com") is False, (
        "a domain rule must never match by mere string suffix -- @corp.com must not catch notcorp.com"
    )
    assert matches("boss@corp.com", '"Boss" <boss@corp.com>') is True, (
        "matches() must parse the address out of a display-name From header"
    )

    # 10. rules.enforce precedence: an email matching both always_action and
    # a model verdict of worth_reading must be MOVED to needs_action, keeping
    # the model's own note -- not overwritten with the generic rule note.
    rules_cfg = Config(
        delivery="email", rules={"always_ignore": [], "always_surface": [], "always_action": ["sender0@example.com"]}
    )
    rules_emails = [_email(0)]
    moved = enforce(
        rules_cfg, rules_emails, [{**_triaged_needs_action(0), "bucket": "worth_reading", "note": "model's own note"}]
    )
    assert len(moved) == 1 and moved[0]["bucket"] == "needs_action" and moved[0]["note"] == "model's own note", (
        "always_action must move a worth_reading item to needs_action while keeping the model's note"
    )

    # 11. schedule.due(): the hourly-workflow gate. cheap, no I/O -- catches a
    # sign error here instead of a fork that never fires (or fires every hour).
    sched_cfg = Config(delivery="email", run_at=["08:00", "18:00"])
    assert due(sched_cfg, datetime(2026, 8, 28, 8, 30, tzinfo=timezone.utc)) == "digest", (
        "a run_at slot, evaluated 30 minutes late (normal cron drift), must still be due"
    )
    assert due(sched_cfg, datetime(2026, 8, 28, 9, 30, tzinfo=timezone.utc)) is None, (
        "90 minutes past a run_at slot must no longer be due, or the digest fires all day"
    )
    assert due(sched_cfg, datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc), event="workflow_dispatch") == "digest", (
        "workflow_dispatch (a human clicking 'Run workflow') must always be due, gate or no gate"
    )
    assert max_gap_hours(["08:00", "18:00"]) == 14, (
        "max_gap_hours wrap-around math regressed -- this backs the window_hours warning"
    )

    print("self-check: ok")
