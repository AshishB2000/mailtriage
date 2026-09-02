"""rules.py is the reader's hard-rule layer: deterministic, no API call.
apply_ignore runs before triage(), enforce runs after pick() -- see cli.run().
"""

from __future__ import annotations

import pytest

from mailtriage.config import Config
from mailtriage.models import Email, Triaged
from mailtriage.rules import apply_ignore, enforce, matches


def make_email(i: int, from_: str | None = None) -> Email:
    return {
        "account": f"acct{i}",
        "from": from_ if from_ is not None else f"sender{i}@example.com",
        "subject": f"real subject {i}",
        "snippet": f"real snippet {i}",
        "body": f"real body {i}",
        "date": "2026-08-28T10:00:00+00:00",
        "unread": True,
        "link": f"https://real.example.com/{i}",
        "message_id": f"<real-{i}@example.com>",
        "reply_to": from_ if from_ is not None else f"sender{i}@example.com",
    }


def make_triaged(i: int, bucket: str = "needs_action", note: str = "model note", from_: str | None = None) -> Triaged:
    em = make_email(i, from_)
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


def cfg_with_rules(**rules: list[str]) -> Config:
    base: dict[str, list[str]] = {"always_ignore": [], "always_surface": [], "always_action": []}
    base.update(rules)
    return Config(delivery="email", rules=base)


# --- matches() -------------------------------------------------------------


@pytest.mark.parametrize(
    "entry, from_header, expected",
    [
        ("boss@corp.com", "boss@corp.com", True),
        ("boss@corp.com", "BOSS@Corp.com", True),  # case-insensitive
        ("boss@corp.com", "other@corp.com", False),
        ("@corp.com", "x@corp.com", True),
        ("@corp.com", "x@mail.corp.com", True),  # subdomain
        ("@corp.com", "x@deep.mail.corp.com", True),  # nested subdomain
        ("@corp.com", "x@notcorp.com", False),  # never a mere suffix
        ("@corp.com", "x@corp.com.evil.com", False),
        ("boss@corp.com", '"Boss" <boss@corp.com>', True),  # display-name From header
        ("@corp.com", '"Team" <ops@mail.corp.com>', True),
    ],
)
def test_matches(entry, from_header, expected):
    assert matches(entry, from_header) is expected


# --- apply_ignore ------------------------------------------------------------


def test_apply_ignore_drops_matches():
    emails = [make_email(0, "spam@junk.com"), make_email(1, "friend@example.com")]
    cfg = cfg_with_rules(always_ignore=["spam@junk.com"])
    kept = apply_ignore(cfg, emails)
    assert [e["from"] for e in kept] == ["friend@example.com"]


def test_apply_ignore_no_op_when_empty():
    emails = [make_email(0)]
    cfg = cfg_with_rules()
    assert apply_ignore(cfg, emails) == emails


def test_apply_ignore_domain_rule():
    emails = [make_email(0, "x@mail.corp.com"), make_email(1, "x@notcorp.com")]
    cfg = cfg_with_rules(always_ignore=["@corp.com"])
    kept = apply_ignore(cfg, emails)
    assert [e["from"] for e in kept] == ["x@notcorp.com"]


def test_apply_ignore_action_beats_ignore():
    """An address matching both always_ignore and always_action must survive
    apply_ignore -- action wins, so the email must still reach enforce()."""
    emails = [make_email(0, "boss@corp.com")]
    cfg = cfg_with_rules(always_ignore=["@corp.com"], always_action=["boss@corp.com"])
    kept = apply_ignore(cfg, emails)
    assert kept == emails


# --- enforce -----------------------------------------------------------------


def test_enforce_no_op_when_no_rules():
    emails = [make_email(0)]
    kept = [make_triaged(0, bucket="worth_reading")]
    cfg = cfg_with_rules()
    assert enforce(cfg, emails, kept) == kept


def test_enforce_always_action_adds_missing_item():
    emails = [make_email(0, "boss@corp.com"), make_email(1)]
    cfg = cfg_with_rules(always_action=["boss@corp.com"])
    result = enforce(cfg, emails, [])
    assert len(result) == 1
    assert result[0]["bucket"] == "needs_action"
    assert result[0]["note"] == "rule: always action from boss@corp.com"
    assert result[0]["idx"] == 0


def test_enforce_always_action_moves_worth_reading_keeping_model_note():
    emails = [make_email(0, "boss@corp.com")]
    kept = [make_triaged(0, bucket="worth_reading", note="model said fyi", from_="boss@corp.com")]
    cfg = cfg_with_rules(always_action=["boss@corp.com"])
    result = enforce(cfg, emails, kept)
    assert len(result) == 1
    assert result[0]["bucket"] == "needs_action"
    assert result[0]["note"] == "model said fyi"


def test_enforce_always_action_leaves_needs_action_alone():
    emails = [make_email(0, "boss@corp.com")]
    kept = [make_triaged(0, bucket="needs_action", note="model's action note", from_="boss@corp.com")]
    cfg = cfg_with_rules(always_action=["boss@corp.com"])
    result = enforce(cfg, emails, kept)
    assert len(result) == 1
    assert result[0]["note"] == "model's action note"


def test_enforce_always_surface_adds_absent_item():
    emails = [make_email(0, "vip@example.com")]
    cfg = cfg_with_rules(always_surface=["vip@example.com"])
    result = enforce(cfg, emails, [])
    assert len(result) == 1
    assert result[0]["bucket"] == "worth_reading"
    assert result[0]["note"] == "rule: always surface from vip@example.com"


def test_enforce_always_surface_bypasses_reading_count():
    """always_surface items must not be crowded out by reading_count -- pick()
    already applied that cap upstream; enforce() adds on top of it."""
    emails = [make_email(i) for i in range(9)]
    emails[8] = make_email(8, "vip@example.com")
    kept = [make_triaged(i, bucket="worth_reading", note=f"n{i}") for i in range(8)]  # already at the cap
    cfg = cfg_with_rules(always_surface=["vip@example.com"])
    result = enforce(cfg, emails, kept)
    assert len(result) == 9
    assert any(t["idx"] == 8 and t["note"] == "rule: always surface from vip@example.com" for t in result)


def test_enforce_always_surface_does_not_touch_already_present_item():
    emails = [make_email(0, "vip@example.com")]
    kept = [make_triaged(0, bucket="needs_action", note="model's note", from_="vip@example.com")]
    cfg = cfg_with_rules(always_surface=["vip@example.com"])
    result = enforce(cfg, emails, kept)
    assert len(result) == 1
    assert result[0]["bucket"] == "needs_action"
    assert result[0]["note"] == "model's note"


def test_enforce_dedupes_by_idx():
    emails = [make_email(0, "boss@corp.com")]
    kept = [make_triaged(0, bucket="needs_action", note="dup1", from_="boss@corp.com")]
    cfg = cfg_with_rules(always_action=["boss@corp.com"], always_surface=["boss@corp.com"])
    result = enforce(cfg, emails, kept)
    assert len(result) == 1


def test_enforce_ordering_needs_action_before_worth_reading():
    emails = [make_email(i, f"s{i}@corp.com") for i in range(4)]
    kept = [
        make_triaged(0, bucket="worth_reading", note="w0", from_="s0@corp.com"),
        make_triaged(2, bucket="worth_reading", note="w2", from_="s2@corp.com"),
    ]
    cfg = cfg_with_rules(always_action=["s1@corp.com"], always_surface=["s3@corp.com"])
    result = enforce(cfg, emails, kept)
    assert [t["bucket"] for t in result] == ["needs_action", "worth_reading", "worth_reading", "worth_reading"]


def test_enforce_forced_item_produces_a_digest_even_when_pick_returned_nothing():
    """A rule-forced needs_action item must still come back even if the model
    (pick()) returned an empty list -- the cli early-return must not fire
    before enforce() runs."""
    emails = [make_email(0, "boss@corp.com")]
    cfg = cfg_with_rules(always_action=["boss@corp.com"])
    result = enforce(cfg, emails, [])
    assert len(result) == 1
