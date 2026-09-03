"""weekly.py: the model-written opening of the weekly review. The prompt is
snapshot-pinned like the triage and draft prompts; narrate_week treats the
reply as hostile input, like pick() and generate_drafts."""

from __future__ import annotations

from datetime import date
from typing import Any, cast

from mailtriage.config import Config
from mailtriage.models import WeekItem, WeekResult
from mailtriage.weekly import WEEK_SCHEMA, WEEK_SYSTEM, build_week_user, narrate_week

CFG = Config(delivery="email")
TODAY = date(2026, 9, 6)


def _item(subject: str, sender: str = "Boss <boss@corp.com>", age: int = 3, day: int = 1) -> WeekItem:
    return cast(
        WeekItem,
        {
            "account": "me@gmail.com",
            "sender": sender,
            "subject": subject,
            "date": f"2026-09-0{day}T10:00:00+00:00",
            "link": "https://mail.google.com/x",
            "age_days": age,
        },
    )


WEEK: WeekResult = {
    "accounts": {
        "me@gmail.com": {
            "replied": [_item("Contract", "Priya <p@x.com>", 2, 4)],
            "archived": [],
            "open": [_item("Newer open", age=1, day=5), _item("Budget sign-off", age=5, day=1)],
        }
    },
    "warnings": [],
}


def test_build_week_user_lists_counts_open_items_oldest_first_with_age_and_done_count():
    user = build_week_user(WEEK, 2, TODAY)
    assert user.startswith("Week ending Sunday 2026-09-06. Marked done via label (subjects not available): 2.")
    assert '<account addr="me@gmail.com">' in user
    assert "replied: 1 · archived: 0 · open: 2" in user
    assert user.index("Budget sign-off · Boss <boss@corp.com> · 5d open") < user.index("Newer open · Boss")
    assert "REPLIED:\n  - Contract · Priya <p@x.com>\n" in user
    assert "ARCHIVED:\n  (none)" in user


def test_narrate_week_passes_the_schema_and_validates_the_reply():
    seen: list[Any] = []

    def call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        seen.append((system, schema))
        return {
            "summary": "  Three sentences.  ",
            "patterns": ["Boss waits", 7, "", "  x  ", "four", "five"],
            "extra": "ignored",
        }

    got = narrate_week(CFG, call, WEEK, 0, TODAY)
    assert seen == [(WEEK_SYSTEM, WEEK_SCHEMA)]
    assert got == {"summary": "Three sentences.", "patterns": ["Boss waits", "x", "four"]}


def test_narrate_week_returns_none_for_an_unusable_reply():
    bad: list[dict[str, Any]] = [{}, {"summary": "", "patterns": []}, {"summary": 3, "patterns": []}, {"items": []}]
    for reply in bad:
        assert narrate_week(CFG, lambda *a, _r=reply: _r, WEEK, 0, TODAY) is None
    # patterns not a list -> summary still stands, patterns empty
    assert narrate_week(CFG, lambda *a: {"summary": "ok", "patterns": "no"}, WEEK, 0, TODAY) == {
        "summary": "ok",
        "patterns": [],
    }


def test_week_schema_is_strict():
    assert WEEK_SCHEMA["additionalProperties"] is False
    assert set(WEEK_SCHEMA["required"]) == set(WEEK_SCHEMA["properties"]) == {"summary", "patterns"}


_WEEK_SYSTEM_SNAPSHOT = """You write the opening of one person's weekly email review. Below is what their triage flagged as needing action this week, per Gmail account, and what became of each item: replied, archived, marked done, or still open (with sender and age in days).

WRITE
- summary: exactly three sentences, second person, plain text. First: what got cleared. Second: what is aging -- the oldest open items and how long. Third: who keeps not getting an answer -- the sender(s) with the most open or oldest items.
- patterns: up to three one-line observations worth acting on (a sender who always waits, a kind of request that piles up, a day nothing moved). Fewer is fine; an empty list is fine. No praise, no filler, no advice to "stay on top of things".

RULES
- Use only the senders, subjects and counts given. Never invent a name, a number or a reason.
- No headings, no bullet characters, no emoji. Name people by the name in the sender field."""


def test_week_system_prompt_is_pinned():
    """Change the prompt deliberately: update this snapshot in the same commit."""
    assert WEEK_SYSTEM == _WEEK_SYSTEM_SNAPSHOT
