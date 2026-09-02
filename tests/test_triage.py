"""triage.pick is the security layer: every hostile-model case must be handled
here, without a network round trip, no matter which of the five backends
produced the reply. Backend-specific plumbing (claude_api, claude_cli,
codex_cli, openai_api, gemini_api) and provider selection have their own test
files; this one covers the shared pieces: build_system, build_user, pick, and
the triage() -> select_backend -> call -> pick wiring.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mailtriage import triage
from mailtriage.config import Config
from mailtriage.models import Email

CFG = Config(delivery="email", interests="rockets and clocks", reading_count=8)


def make_email(i: int) -> Email:
    return {
        "account": f"acct{i}",
        "from": f"sender{i}@example.com",
        "subject": f"real subject {i}",
        "snippet": f"real snippet {i}",
        "body": f"real body {i}",
        "date": "2026-08-28T10:00:00+00:00",
        "unread": True,
        "link": f"https://real.example.com/{i}",
        "message_id": f"<real-{i}@example.com>",
        "reply_to": f"sender{i}@example.com",
        "uid": f"{i}",
    }


def test_pick_drops_hostile_ids_and_dedupes():
    emails = [make_email(i) for i in range(5)]
    reply = {
        "items": [
            {"id": True, "bucket": "needs_action", "note": "bool id, not an int"},
            {"id": "2", "bucket": "needs_action", "note": "string id"},
            {"id": 99, "bucket": "needs_action", "note": "out of range"},
            {"id": -1, "bucket": "needs_action", "note": "negative"},
            {"id": 0, "bucket": "needs_action", "note": "first, kept"},
            {"id": 0, "bucket": "needs_action", "note": "duplicate of above, dropped"},
            {"id": 1, "bucket": "noise", "note": "unknown bucket, dropped"},
        ]
    }
    picked = triage.pick(CFG, emails, reply)
    assert len(picked) == 1
    assert picked[0]["note"] == "first, kept"


def test_pick_caps_worth_reading_but_not_needs_action():
    emails = [make_email(i) for i in range(20)]
    items = [{"id": i, "bucket": "worth_reading", "note": f"n{i}"} for i in range(12)]
    items += [{"id": i, "bucket": "needs_action", "note": f"a{i}"} for i in range(12, 15)]
    reply = {"items": items}
    picked = triage.pick(CFG, emails, reply)
    worth = [p for p in picked if p["bucket"] == "worth_reading"]
    action = [p for p in picked if p["bucket"] == "needs_action"]
    assert len(worth) == 8  # cfg.reading_count
    assert len(action) == 3  # uncapped


def test_pick_coerces_null_note_to_empty_string():
    """An explicit JSON null (not just a missing key) must not become the string "None"."""
    emails = [make_email(0)]
    reply = {"items": [{"id": 0, "bucket": "needs_action", "note": None}]}
    picked = triage.pick(CFG, emails, reply)
    assert picked[0]["note"] == ""


def test_pick_ignores_model_supplied_fields_uses_real_email():
    emails = [make_email(0)]
    reply = {
        "items": [
            {
                "id": 0,
                "bucket": "needs_action",
                "note": "reply by Friday",
                "link": "https://evil.example.com/phish",
                "subject": "fabricated subject",
            }
        ]
    }
    picked = triage.pick(CFG, emails, reply)
    assert picked[0]["link"] == emails[0]["link"]
    assert picked[0]["subject"] == emails[0]["subject"]
    assert picked[0]["sender"] == emails[0]["from"]
    assert picked[0]["note"] == "reply by Friday"


def test_pick_sorts_needs_action_first_preserving_order():
    emails = [make_email(i) for i in range(4)]
    reply = {
        "items": [
            {"id": 0, "bucket": "worth_reading", "note": "w0"},
            {"id": 1, "bucket": "needs_action", "note": "a1"},
            {"id": 2, "bucket": "worth_reading", "note": "w2"},
            {"id": 3, "bucket": "needs_action", "note": "a3"},
        ]
    }
    picked = triage.pick(CFG, emails, reply)
    assert [p["bucket"] for p in picked] == ["needs_action", "needs_action", "worth_reading", "worth_reading"]
    assert [p["note"] for p in picked] == ["a1", "a3", "w0", "w2"]


def test_build_system_has_interests_and_bucket_names():
    system = triage.build_system(CFG)
    assert "rockets and clocks" in system
    assert "needs_action" in system
    assert "worth_reading" in system


# Snapshot of build_system's output for CFG, captured from the source BEFORE
# accounts/rules/draft_style existed. A default config (no cfg.accounts) must
# keep producing exactly this -- adding the per-account feature must not
# change the prompt for every fork that never uses it.
_BUILD_SYSTEM_SNAPSHOT = f"""You are triaging one person's email inbox. Below are the messages that arrived recently. Sort them into buckets, or leave them out entirely.

<interests>
{CFG.interests}
</interests>

<avoid>
{CFG.avoid}
</avoid>

BUCKETS
- needs_action: the reader must DO something about this message — a reply is expected, a bill or payment is due, there's a deadline or RSVP, something is expiring, or someone asked them a direct question. If you cannot name the concrete action, it is NOT needs_action — demote it to worth_reading, or leave it out.
- worth_reading: a real human or real content worth a glance, with nothing the reader needs to do about it.
- Anything else — newsletters, promotions, receipts, automated notifications — is noise. Do not return noise at all. There is no third bucket for it; simply omit it.

HOW MANY TO RETURN
- needs_action has no cap. Never drop a message that genuinely needs action just to keep the list short.
- worth_reading: return at most {CFG.reading_count}, and you are explicitly permitted — and expected — to return fewer. Most windows do not contain {CFG.reading_count} things worth reading; feeds and mailing lists post on their own schedule, not this reader's. An honest short list beats a padded one: if a worth_reading item is only there to reach {CFG.reading_count}, leaving it out makes the digest strictly better. Padding is the failure that kills this product — it trains the reader to stop opening it. Returning an empty worth_reading list is valid and correct.

WRITING
- note: one line. For needs_action, name the concrete action the reader must take. For worth_reading, say why it's worth a glance. No hedging, no "this could mean big things".
- Copy each message's bracketed integer id EXACTLY as given. Never invent an id, and never address a message by anything other than its bracketed integer."""


def test_build_system_default_config_is_byte_identical_to_pre_accounts_snapshot():
    assert CFG.accounts == {}
    assert triage.build_system(CFG) == _BUILD_SYSTEM_SNAPSHOT


def test_build_system_omits_per_account_section_by_default():
    assert "PER-ACCOUNT" not in triage.build_system(CFG)


def test_build_system_adds_per_account_context_section():
    cfg = Config(
        delivery="email",
        interests="rockets and clocks",
        accounts={"work@corp.com": {"interests": "eng-leads mailing list", "avoid": "internal memes"}},
    )
    system = triage.build_system(cfg)
    assert "PER-ACCOUNT CONTEXT" in system
    assert '<account addr="work@corp.com">' in system
    assert "eng-leads mailing list" in system
    assert "internal memes" in system
    # base prompt content must still be present, verbatim
    assert "rockets and clocks" in system
    assert "needs_action" in system


def test_build_system_per_account_omits_empty_subblocks():
    cfg = Config(delivery="email", accounts={"work@corp.com": {"interests": "eng-leads"}})
    system = triage.build_system(cfg)
    account_block = system.split('<account addr="work@corp.com">', 1)[1]
    assert "<interests>" in account_block
    assert "<avoid>" not in account_block


def test_build_system_per_account_skips_account_with_nothing_to_add():
    cfg = Config(delivery="email", accounts={"work@corp.com": {}})
    system = triage.build_system(cfg)
    assert "PER-ACCOUNT" not in system


def test_build_user_has_bracketed_index():
    now = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    user = triage.build_user([make_email(0), make_email(1)], now)
    assert "[0]" in user
    assert "[1]" in user


def test_triage_end_to_end_uses_selected_backend(monkeypatch):
    """triage() must call select_backend(), pass it build_system/build_user/
    TRIAGE_SCHEMA, and run the reply through pick() -- wired together, not
    unit by unit."""
    emails = [make_email(0)]
    sentinel: dict[str, Any] = {"items": [{"id": 0, "bucket": "needs_action", "note": "via stub backend"}]}
    seen: dict[str, str] = {}

    def fake_call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        seen["system"] = system
        seen["user"] = user
        assert schema == triage.TRIAGE_SCHEMA
        return sentinel

    def fake_select_backend(cfg: Config, environ: Any) -> tuple[str, Any]:
        return "stub", fake_call

    monkeypatch.setattr(triage, "select_backend", fake_select_backend)
    result = triage.triage(CFG, emails, datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
    assert result[0]["note"] == "via stub backend"
    assert "rockets and clocks" in seen["system"]
    assert "[0]" in seen["user"]
