"""The triage prompt is the product. Everything else is plumbing.

Do not condense the prompt below. "Return fewer, never pad" is stated three
times — permission, justification, consequence — because models treat a
stated count as a target, and one polite "you may return fewer" gets ignored.

HARD CONSTRAINT: this module must not import `anthropic` at module scope.
selfcheck.py imports `pick` from here and must work with `anthropic`
uninstalled — so the import (and its exception classes) lives inside `_call`.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from mailtriage.config import Config
from mailtriage.errors import MailError
from mailtriage.models import Email, Triaged

# Headline triage running on the *user's* bill. Do not upgrade to Opus
# without a reason.
MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000
BUCKETS = ("needs_action", "worth_reading")

TOOL: dict[str, Any] = {
    "name": "emit_triage",
    "description": "Return the bucketed, annotated emails worth this reader's attention.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "Triaged emails. May be empty.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "integer",
                            "description": "The bracketed index of the email, copied exactly.",
                        },
                        "bucket": {
                            "type": "string",
                            "enum": list(BUCKETS),
                        },
                        "note": {
                            "type": "string",
                            "description": "One line: for needs_action, the concrete action; for worth_reading, why it's worth a glance.",
                        },
                    },
                    "required": ["id", "bucket", "note"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    },
}

# Forced, not suggested: the whole reply shape depends on this tool being called.
TOOL_CHOICE = {"type": "tool", "name": "emit_triage"}


def build_system(cfg: Config) -> str:
    return f"""You are triaging one person's email inbox. Below are the messages that arrived recently. Sort them into buckets, or leave them out entirely.

<interests>
{cfg.interests}
</interests>

<avoid>
{cfg.avoid}
</avoid>

BUCKETS
- needs_action: the reader must DO something about this message — a reply is expected, a bill or payment is due, there's a deadline or RSVP, something is expiring, or someone asked them a direct question. If you cannot name the concrete action, it is NOT needs_action — demote it to worth_reading, or leave it out.
- worth_reading: a real human or real content worth a glance, with nothing the reader needs to do about it.
- Anything else — newsletters, promotions, receipts, automated notifications — is noise. Do not return noise at all. There is no third bucket for it; simply omit it.

HOW MANY TO RETURN
- needs_action has no cap. Never drop a message that genuinely needs action just to keep the list short.
- worth_reading: return at most {cfg.reading_count}, and you are explicitly permitted — and expected — to return fewer. Most windows do not contain {cfg.reading_count} things worth reading; feeds and mailing lists post on their own schedule, not this reader's. An honest short list beats a padded one: if a worth_reading item is only there to reach {cfg.reading_count}, leaving it out makes the digest strictly better. Padding is the failure that kills this product — it trains the reader to stop opening it. Returning an empty worth_reading list is valid and correct.

WRITING
- note: one line. For needs_action, name the concrete action the reader must take. For worth_reading, say why it's worth a glance. No hedging, no "this could mean big things".
- Copy each message's bracketed integer id EXACTLY as given. Never invent an id, and never address a message by anything other than its bracketed integer."""


def build_user(emails: list[Email], now: datetime) -> str:
    blocks = []
    for i, em in enumerate(emails):
        sent = datetime.fromisoformat(em["date"])
        if sent.tzinfo is not None and now.tzinfo is None:
            now = now.astimezone(sent.tzinfo)
        elif sent.tzinfo is None and now.tzinfo is not None:
            sent = sent.replace(tzinfo=now.tzinfo)
        age_minutes = max(0, int((now - sent).total_seconds() // 60))
        if age_minutes < 60:
            age = f"{age_minutes}m ago"
        elif age_minutes < 24 * 60:
            age = f"{age_minutes // 60}h ago"
        else:
            age = f"{age_minutes // (24 * 60)}d ago"
        status = "UNREAD" if em["unread"] else "read"
        blocks.append(
            f"[{i}] {em['subject']}\n    from: {em['from']} · {em['account']} · {age} · {status}\n    {em['snippet']}"
        )
    return "Emails:\n\n" + "\n\n".join(blocks)


def _call(cfg: Config, emails: list[Email], now: datetime) -> Any:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise MailError(
            "ANTHROPIC_API_KEY is not set. Add it in your fork: Settings -> Secrets and variables "
            "-> Actions -> New repository secret, named exactly ANTHROPIC_API_KEY. "
            "Get a key at https://console.anthropic.com/settings/keys"
        )
    import anthropic

    try:
        return anthropic.Anthropic().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=build_system(cfg),
            messages=[{"role": "user", "content": build_user(emails, now)}],
            tools=[TOOL],
            tool_choice=TOOL_CHOICE,
        )
    except anthropic.AuthenticationError as e:
        raise MailError(
            "Anthropic rejected ANTHROPIC_API_KEY. The secret is set but the key is wrong, revoked, or has a "
            "stray space. Make a fresh key at https://console.anthropic.com/settings/keys and update the "
            "ANTHROPIC_API_KEY secret under Settings -> Secrets and variables -> Actions."
        ) from e
    except anthropic.RateLimitError as e:
        raise MailError(
            "Anthropic rate-limited this run, or the account is out of credit. Nothing to fix in the code — "
            "check the balance at https://console.anthropic.com/settings/billing; the next scheduled run "
            "will pick things up."
        ) from e
    except anthropic.APIStatusError as e:
        raise MailError(
            f"Anthropic returned HTTP {e.status_code}: {e.message} — an API-side error, not a config error. "
            "Re-run the workflow by hand; if it keeps happening check https://status.anthropic.com"
        ) from e
    except anthropic.APIConnectionError as e:
        raise MailError(
            f"could not reach api.anthropic.com ({e}). The runner had no network or DNS failed. "
            "Re-run the workflow by hand."
        ) from e


def pick(cfg: Config, emails: list[Email], reply: dict[str, Any]) -> list[Triaged]:
    """Map the model's bucketed ids back onto the real emails.

    Split out from the API call so it can be tested without a network round
    trip — this is where every hostile-model case is handled.
    """
    needs_action: list[Triaged] = []
    worth_reading: list[Triaged] = []
    seen: set[int] = set()
    for got in reply.get("items", []):
        i = got.get("id")
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(emails) or i in seen:
            continue
        bucket = got.get("bucket")
        if bucket not in BUCKETS:
            continue
        if bucket == "worth_reading" and len(worth_reading) >= cfg.reading_count:
            continue
        seen.add(i)
        src = emails[i]
        triaged = Triaged(
            bucket=bucket,
            note=str(got.get("note", "")),
            account=src["account"],
            sender=src["from"],
            subject=src["subject"],
            link=src["link"],
            date=src["date"],
            unread=src["unread"],
        )
        if bucket == "needs_action":
            needs_action.append(triaged)
        else:
            worth_reading.append(triaged)
    return needs_action + worth_reading


def triage(cfg: Config, emails: list[Email], now: datetime) -> list[Triaged]:
    resp = _call(cfg, emails, now)

    if resp.stop_reason == "max_tokens":
        raise MailError(
            "the model's reply was cut off before it finished (stop_reason=max_tokens). Lower 'reading_count' "
            "in config.yaml, or shorten the 'interests' text."
        )
    block = next((b for b in resp.content if b.type == "tool_use"), None)
    if block is None:
        raise MailError(
            "the model returned no triage at all, which usually means it declined the request. Check the "
            "'interests' and 'avoid' text in config.yaml for anything it might refuse to act on."
        )
    return pick(cfg, emails, block.input)
