"""The weekly review's model-written opening: three sentences on what got
cleared, what is aging, and who keeps not getting an answer, plus up to
three one-line patterns. Same hostile-input discipline as `drafts.py`: the
reply is validated, never trusted structurally, and a model hiccup degrades
to the plain review -- `cli.run_weekly` catches the MailError.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from mailtriage.config import Config
from mailtriage.models import WeekItem, WeekResult
from mailtriage.triage import CallFn

MAX_PATTERNS = 3

# The two numbers behind the weekly "time saved" line, in one place because
# they are an ESTIMATE, not a measurement: nothing times the reader. 1.5 =
# roughly what opening, reading and deciding on one message costs when you do
# it yourself; 4 = writing a reply from scratch instead of editing a draft.
# The README says exactly this, in these words -- change both together.
MINUTES_PER_TRIAGED = 1.5
MINUTES_PER_DRAFT = 4.0


def minutes_saved(triaged: int, drafts: int) -> int:
    return round(triaged * MINUTES_PER_TRIAGED + drafts * MINUTES_PER_DRAFT)


def week_totals(week: WeekResult, done_count: int, drafts: int) -> dict[str, int]:
    """The "This week mailtriage handled ..." numbers. No run stores a count of
    what it triaged, so the week's total is reconstructed from Gmail: every
    message still carrying the action label (pull_week's three buckets), plus
    the ones closed with the done label, which have lost it."""
    labeled = sum(len(b["replied"]) + len(b["archived"]) + len(b["open"]) for b in week["accounts"].values())
    triaged = labeled + done_count
    return {"triaged": triaged, "drafts": drafts, "minutes": minutes_saved(triaged, drafts)}


WEEK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Three sentences: what got cleared, what is aging, who keeps not getting an answer.",
        },
        "patterns": {
            "type": "array",
            "description": "Up to three one-line patterns worth noticing. May be empty.",
            "items": {"type": "string"},
        },
    },
    "required": ["summary", "patterns"],
    "additionalProperties": False,
}

WEEK_SYSTEM = """You write the opening of one person's weekly email review. Below is what their triage flagged as needing action this week, per Gmail account, and what became of each item: replied, archived, marked done, or still open (with sender and age in days).

WRITE
- summary: exactly three sentences, second person, plain text. First: what got cleared. Second: what is aging -- the oldest open items and how long. Third: who keeps not getting an answer -- the sender(s) with the most open or oldest items.
- patterns: up to three one-line observations worth acting on (a sender who always waits, a kind of request that piles up, a day nothing moved). Fewer is fine; an empty list is fine. No praise, no filler, no advice to "stay on top of things".

RULES
- Use only the senders, subjects and counts given. Never invent a name, a number or a reason.
- No headings, no bullet characters, no emoji. Name people by the name in the sender field."""


def _lines(items: list[WeekItem], with_age: bool) -> str:
    if not items:
        return "  (none)"
    out = []
    for it in sorted(items, key=lambda i: i["date"]):  # oldest first
        line = f"  - {it['subject']} · {it['sender']}"
        if with_age:
            line += f" · {it['age_days']}d open"
        out.append(line)
    return "\n".join(out)


def build_week_user(week: WeekResult, done_count: int, today: date) -> str:
    blocks = []
    for account, b in week["accounts"].items():
        blocks.append(
            f'<account addr="{account}">\n'
            f"replied: {len(b['replied'])} · archived: {len(b['archived'])} · open: {len(b['open'])}\n"
            f"OPEN:\n{_lines(list(b['open']), True)}\n"
            f"REPLIED:\n{_lines(list(b['replied']), False)}\n"
            f"ARCHIVED:\n{_lines(list(b['archived']), False)}\n"
            "</account>"
        )
    head = f"Week ending {today:%A %Y-%m-%d}. Marked done via label (subjects not available): {done_count}."
    return head + "\n\n" + "\n\n".join(blocks)


def narrate_week(cfg: Config, call: CallFn, week: WeekResult, done_count: int, today: date) -> dict[str, Any] | None:
    """{"summary": str, "patterns": [str]} or None when the reply is unusable.
    A MailError from `call` itself (auth, network) is NOT caught here."""
    reply = call(cfg, WEEK_SYSTEM, build_week_user(week, done_count, today), WEEK_SCHEMA)
    summary = reply.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    raw = reply.get("patterns")
    patterns = [p.strip()[:300] for p in raw if isinstance(p, str) and p.strip()] if isinstance(raw, list) else []
    return {"summary": summary.strip()[:2000], "patterns": patterns[:MAX_PATTERNS]}
