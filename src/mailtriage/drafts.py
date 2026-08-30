"""AI-drafted replies for needs_action items — the digest shows them, and
`imap_pull.push_drafts` appends them to Gmail Drafts. This module never sends
anything; it only decides what a draft should say.

Same hostile-input discipline as `triage.pick` (read that one first): the
model's reply is treated as adversarial input, never trusted structurally.
"""

from __future__ import annotations

from typing import Any

from mailtriage.config import Config
from mailtriage.models import Email, Triaged
from mailtriage.triage import CallFn

DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "Drafted replies. May be empty -- skip any message a reply isn't the right action for.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "The bracketed index of the email, copied exactly.",
                    },
                    "draft": {
                        "type": "string",
                        "description": "The drafted reply: plain text, ready to send after a quick human read.",
                    },
                },
                "required": ["id", "draft"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def build_draft_system(cfg: Config) -> str:
    return f"""You are drafting reply emails on behalf of one person, for the messages below that need a reply from them.

<about-the-reader>
{cfg.interests}
</about-the-reader>

RULES
- Write in plain text, ready to send after a quick human read.
- Match the sender's language and register -- formal stays formal, casual stays casual.
- NEVER invent facts, dates, prices, commitments, or attachments that are not present in the source email. When a required detail is unknown, leave a bracketed [like this] placeholder instead of guessing.
- Keep it short: a few sentences, unless the email genuinely demands more.
- No signature block beyond a simple sign-off -- omit the reader's first name, end with "Thanks," (or the language-appropriate equivalent) on its own line.
- Reply only to messages by their bracketed integer id, copied exactly. Never invent an id.
- You may return fewer items than given: skip any message where a reply obviously isn't the right action (e.g. "pay this bill") by omitting it."""


def build_draft_user(emails: list[Email], triaged_needs_action: list[Triaged]) -> str:
    blocks = []
    for t in triaged_needs_action:
        src = emails[t["idx"]]
        body = src["body"][:8000]  # defensive re-truncation -- parse_message already caps this
        blocks.append(f"[{t['idx']}] {src['subject']}\n    from: {src['from']} · action: {t['note']}\n\n{body}")
    return "Emails:\n\n" + "\n\n".join(blocks)


def generate_drafts(cfg: Config, call: CallFn, emails: list[Email], triaged: list[Triaged]) -> None:
    """Mutate `triaged` in place, filling `draft` on needs_action items the
    model chose to draft for. Makes zero calls when nothing needs action.

    On a malformed model reply, invalid entries are dropped silently -- a
    digest without drafts beats no digest. A MailError from `call` itself
    (auth, network, rate limit) is not caught here: it must surface.
    """
    needs_action = [t for t in triaged if t["bucket"] == "needs_action"]
    if not needs_action:
        return

    reply = call(cfg, build_draft_system(cfg), build_draft_user(emails, needs_action), DRAFT_SCHEMA)

    valid_idx = {t["idx"] for t in needs_action}
    drafts_by_idx: dict[int, str] = {}
    for got in reply.get("items", []):
        i = got.get("id")
        if not isinstance(i, int) or isinstance(i, bool) or i not in valid_idx or i in drafts_by_idx:
            continue
        d = got.get("draft")
        drafts_by_idx[i] = str(d) if d else ""

    for t in needs_action:
        if t["idx"] in drafts_by_idx:
            t["draft"] = drafts_by_idx[t["idx"]]
