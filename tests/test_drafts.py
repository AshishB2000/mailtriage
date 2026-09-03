"""generate_drafts is the security layer for AI-drafted replies, same
discipline as triage.pick: every hostile-model case must be handled here,
without a network round trip.
"""

from __future__ import annotations

from typing import Any

import pytest

from mailtriage.config import Config
from mailtriage.drafts import DRAFT_SCHEMA, build_draft_system, build_draft_user, draft_schema, generate_drafts
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
        "uid": f"{i}",
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


# Snapshot of build_draft_system's output for CFG, captured from the source
# BEFORE draft_style/accounts existed. A default config (draft_style at its
# defaults, no accounts) must keep producing exactly this.
_BUILD_DRAFT_SYSTEM_SNAPSHOT = f"""You are drafting reply emails on behalf of one person, for the messages below that need a reply from them.

<about-the-reader>
{CFG.interests}
</about-the-reader>

RULES
- Write in plain text, ready to send after a quick human read.
- Match the sender's language and register -- formal stays formal, casual stays casual.
- NEVER invent facts, dates, prices, commitments, or attachments that are not present in the source email. When a required detail is unknown, leave a bracketed [like this] placeholder instead of guessing.
- Keep it short: a few sentences, unless the email genuinely demands more.
- No signature block beyond a simple sign-off -- omit the reader's first name, end with "Thanks," (or the language-appropriate equivalent) on its own line.
- Reply only to messages by their bracketed integer id, copied exactly. Never invent an id.
- You may return fewer items than given: skip any message where a reply obviously isn't the right action (e.g. "pay this bill") by omitting it."""


def test_build_draft_system_default_config_is_byte_identical_to_pre_style_snapshot():
    assert CFG.draft_style == {
        "tone": "friendly",
        "sign_off": "",
        "language": "auto",
        "max_sentences": 5,
        "learn_voice": True,
    }
    assert CFG.accounts == {}
    assert build_draft_system(CFG) == _BUILD_DRAFT_SYSTEM_SNAPSHOT


def test_build_draft_system_unchanged_when_no_voice_examples():
    assert build_draft_system(CFG, {}) == _BUILD_DRAFT_SYSTEM_SNAPSHOT
    assert build_draft_system(CFG, None) == _BUILD_DRAFT_SYSTEM_SNAPSHOT


def test_build_draft_system_learn_voice_off_is_not_a_style_change():
    cfg = Config(delivery="email", interests="rockets and clocks")
    cfg.draft_style = {**cfg.draft_style, "learn_voice": False}
    assert "STYLE" not in build_draft_system(cfg)


def test_build_draft_system_appends_voice_examples_per_item():
    voice = {3: ["Hey Bob,\n\nSure thing, Tuesday works.\n\nCheers,\nA"], 7: ["Dear Ms. Lee,\n\nThank you."]}
    system = build_draft_system(CFG, voice)
    assert system.startswith(_BUILD_DRAFT_SYSTEM_SNAPSHOT)
    tail = system[len(_BUILD_DRAFT_SYSTEM_SNAPSHOT) :]
    assert (
        "VOICE\nExamples of how this person writes to this recipient -- match the tone, length, greeting and sign-off."
        in tail
    )
    assert (
        '<voice for="[3]">\n<example>\nHey Bob,\n\nSure thing, Tuesday works.\n\nCheers,\nA\n</example>\n</voice>'
        in tail
    )
    assert '<voice for="[7]">' in tail and "Dear Ms. Lee" in tail


def test_draft_schema_one_variant_is_the_default_schema():
    assert draft_schema(1) == DRAFT_SCHEMA
    assert set(DRAFT_SCHEMA["properties"]["items"]["items"]["properties"]) == {"id", "draft"}


def test_two_variants_schema_prompt_and_parsing():
    cfg = Config(delivery="email", interests="rockets and clocks", draft_variants=2)
    emails = [make_email(0), make_email(1)]
    triaged = [make_triaged(0), make_triaged(1)]
    seen: dict[str, Any] = {}

    def fake_call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        seen["system"], seen["schema"] = system, schema
        return {"items": [{"id": 0, "short": "Yes, Tuesday.", "full": "Hi,\n\nTuesday works, 10am.\n\nThanks,"}]}

    generate_drafts(cfg, fake_call, emails, triaged)

    item = seen["schema"]["properties"]["items"]["items"]
    assert set(item["properties"]) == {"id", "short", "full"} and set(item["required"]) == {"id", "short", "full"}
    assert "VARIANTS" in seen["system"] and "`short`" in seen["system"] and "`full`" in seen["system"]
    assert triaged[0]["draft"] == "Yes, Tuesday."
    assert triaged[0]["draft_full"] == "Hi,\n\nTuesday works, 10am.\n\nThanks,"
    assert triaged[1]["draft"] == "" and "draft_full" not in triaged[1]


def test_one_variant_prompt_has_no_variants_section():
    assert "VARIANTS" not in build_draft_system(CFG)


def test_generate_drafts_passes_voice_into_system_prompt():
    emails = [make_email(0)]
    triaged = [make_triaged(0)]
    seen: dict[str, str] = {}

    def fake_call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        seen["system"], seen["user"] = system, user
        return {"items": []}

    generate_drafts(CFG, fake_call, emails, triaged, {0: ["Thanks Sam, will do."]})
    assert "Thanks Sam, will do." in seen["system"]
    assert "Thanks Sam, will do." not in seen["user"]


def test_build_draft_system_omits_style_section_by_default():
    system = build_draft_system(CFG)
    assert "STYLE" not in system


def test_build_draft_system_adds_style_section_when_tone_changed():
    cfg = Config(
        delivery="email",
        interests="rockets and clocks",
        draft_style={"tone": "formal", "sign_off": "", "language": "auto", "max_sentences": 5},
    )
    system = build_draft_system(cfg)
    assert "STYLE" in system
    assert "formal" in system.split("STYLE", 1)[1]
    assert "At most 5 sentences unless the email demands more." in system
    # existing rules stay verbatim
    assert "NEVER invent" in system
    assert 'end with "Thanks,"' in system


def test_build_draft_system_style_sign_off_and_language():
    cfg = Config(
        delivery="email",
        draft_style={"tone": "friendly", "sign_off": "Best, Alex", "language": "French", "max_sentences": 3},
    )
    system = build_draft_system(cfg)
    assert "Sign off as: Best, Alex" in system
    assert "Write in French." in system
    assert "At most 3 sentences unless the email demands more." in system


def test_build_draft_system_per_account_style_section():
    cfg = Config(
        delivery="email",
        draft_style={"tone": "friendly", "sign_off": "", "language": "auto", "max_sentences": 5},
        accounts={
            "work@corp.com": {"draft_style": {"tone": "formal", "sign_off": "", "language": "auto", "max_sentences": 5}}
        },
    )
    system = build_draft_system(cfg)
    assert "PER-ACCOUNT STYLE" in system
    assert '<account addr="work@corp.com">' in system
    block = system.split('<account addr="work@corp.com">', 1)[1]
    assert "formal" in block.lower() or "Tone: formal" in block


def test_build_draft_system_no_per_account_style_when_account_has_no_override():
    cfg = Config(delivery="email", accounts={"work@corp.com": {"interests": "eng leads"}})
    system = build_draft_system(cfg)
    assert "PER-ACCOUNT STYLE" not in system
