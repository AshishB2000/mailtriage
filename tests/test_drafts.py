"""generate_drafts is the security layer for AI-drafted replies, same
discipline as triage.pick: every hostile-model case must be handled here,
without a network round trip.
"""

from __future__ import annotations

from typing import Any

import pytest

from mailtriage.config import Config
from mailtriage.drafts import build_draft_system, build_draft_user, generate_drafts
from mailtriage.errors import MailError
from mailtriage.models import Email, Triaged

CFG = Config(delivery="email", interests="rockets and clocks")


def make_email(i: int) -> Email:
    return {
        "account": f"acct{i}",
        "from": f"sender{i}@example.com",
        "subject": f"real subject {i}",
        "snippet": f"real snippet {i}",
        "body": f"real body {i} -- please confirm the meeting time.",
        "date": "2026-08-28T10:00:00+00:00",
        "unread": True,
        "link": f"https://real.example.com/{i}",
        "message_id": f"<real-{i}@example.com>",
        "reply_to": f"sender{i}@example.com",
    }


def make_triaged(i: int, bucket: str = "needs_action", note: str = "reply with a time") -> Triaged:
    em = make_email(i)
    return {
        "bucket": bucket,
        "note": note,
        "account": em["account"],
        "sender": em["from"],
        "subject": em["subject"],
        "link": em["link"],
        "date": em["date"],
        "unread": em["unread"],
        "idx": i,
        "draft": "",
    }


def test_generate_drafts_happy_mapping():
    emails = [make_email(0), make_email(1)]
    triaged = [make_triaged(0), make_triaged(1)]

    def fake_call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {"items": [{"id": 0, "draft": "Works for me, see you then.\n\nThanks,"}]}

    generate_drafts(CFG, fake_call, emails, triaged)
    assert triaged[0]["draft"] == "Works for me, see you then.\n\nThanks,"
    assert triaged[1]["draft"] == ""  # model chose not to draft for this one


def test_generate_drafts_drops_bool_id():
    emails = [make_email(0)]
    triaged = [make_triaged(0)]
    generate_drafts(CFG, lambda *a, **k: {"items": [{"id": True, "draft": "nope"}]}, emails, triaged)
    assert triaged[0]["draft"] == ""


def test_generate_drafts_dedupes_duplicate_id():
    emails = [make_email(0)]
    triaged = [make_triaged(0)]

    def fake_call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {"items": [{"id": 0, "draft": "first"}, {"id": 0, "draft": "duplicate, must be dropped"}]}

    generate_drafts(CFG, fake_call, emails, triaged)
    assert triaged[0]["draft"] == "first"


def test_generate_drafts_drops_unknown_idx():
    """An id that isn't in this batch's needs_action set (out of range, or a
    worth_reading item's idx) must be dropped, never attached anywhere."""
    emails = [make_email(0), make_email(1)]
    triaged = [make_triaged(0), make_triaged(1, bucket="worth_reading", note="fyi")]

    def fake_call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {"items": [{"id": 1, "draft": "should be dropped, idx 1 is worth_reading"}, {"id": 99, "draft": "oob"}]}

    generate_drafts(CFG, fake_call, emails, triaged)
    assert triaged[0]["draft"] == ""
    assert triaged[1]["draft"] == ""


def test_generate_drafts_coerces_null_draft_to_empty_string():
    emails = [make_email(0)]
    triaged = [make_triaged(0)]
    generate_drafts(CFG, lambda *a, **k: {"items": [{"id": 0, "draft": None}]}, emails, triaged)
    assert triaged[0]["draft"] == ""


def test_generate_drafts_makes_zero_calls_when_nothing_needs_action():
    emails = [make_email(0)]
    triaged = [make_triaged(0, bucket="worth_reading", note="fyi")]

    def _boom(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("call must not be invoked when nothing needs action")

    generate_drafts(CFG, _boom, emails, triaged)  # must not raise


def test_generate_drafts_propagates_mail_error_from_call():
    emails = [make_email(0)]
    triaged = [make_triaged(0)]

    def _raises(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        raise MailError("auth failed")

    with pytest.raises(MailError, match="auth failed"):
        generate_drafts(CFG, _raises, emails, triaged)


def test_build_draft_system_states_never_invent_rule():
    system = build_draft_system(CFG)
    assert "NEVER invent" in system
    assert "rockets and clocks" in system


def test_build_draft_user_includes_body_and_note():
    emails = [make_email(0)]
    triaged = [make_triaged(0, note="confirm the meeting time")]
    user = build_draft_user(emails, triaged)
    assert "[0]" in user
    assert "please confirm the meeting time" in user  # from the email body
    assert "confirm the meeting time" in user  # the triage note
