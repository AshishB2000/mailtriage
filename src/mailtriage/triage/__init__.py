"""The triage prompt is the product. Everything else is plumbing.

Do not condense the prompt below. "Return fewer, never pad" is stated three
times — permission, justification, consequence — because models treat a
stated count as a target, and one polite "you may return fewer" gets ignored.

HARD CONSTRAINT: `anthropic` must never be imported at module scope anywhere
in this package. selfcheck.py imports `pick` (and this module imports every
backend module at module scope) and must work with `anthropic` uninstalled —
so that import lives inside `claude_api.call` only.

This package mirrors delivery/'s dict-dispatch pattern: five interchangeable
backends behind one `call(cfg, system, user, schema) -> dict` contract.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from mailtriage.config import Config
from mailtriage.errors import MailError
from mailtriage.models import Email, Triaged
from mailtriage.triage import claude_api, claude_cli, codex_cli, gemini_api, openai_api

BUCKETS = ("needs_action", "worth_reading")

# Same shape every backend must fill in. Claude gets it wrapped as a forced
# tool's input_schema; OpenAI and Gemini get it as a response schema; the two
# CLI backends get it as --output-schema/--json-schema. One definition, five
# transports.
TRIAGE_SCHEMA: dict[str, Any] = {
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
}

CallFn = Callable[[Config, str, str, dict[str, Any]], dict[str, Any]]

# name -> (backend's call(), the env var whose presence selects it in "auto" mode).
# Order is significant: in "auto" mode the first entry whose secret is set wins.
# This is today's ANTHROPIC_API_KEY/CLAUDE_CODE_OAUTH_TOKEN precedence, extended
# — do not reorder, existing forks rely on it.
PROVIDERS: dict[str, tuple[CallFn, str]] = {
    "claude-subscription": (claude_cli.call, "CLAUDE_CODE_OAUTH_TOKEN"),
    "claude-api": (claude_api.call, "ANTHROPIC_API_KEY"),
    "chatgpt-subscription": (codex_cli.call, "CODEX_AUTH_JSON"),
    "openai-api": (openai_api.call, "OPENAI_API_KEY"),
    "gemini-api": (gemini_api.call, "GEMINI_API_KEY"),
}


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


def pick(cfg: Config, emails: list[Email], reply: dict[str, Any]) -> list[Triaged]:
    """Map the model's bucketed ids back onto the real emails.

    Split out from the backend calls so it can be tested without a network
    round trip — this is where every hostile-model case is handled, no
    matter which of the five backends produced the reply.
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
            note=str(n) if (n := got.get("note")) else "",
            account=src["account"],
            sender=src["from"],
            subject=src["subject"],
            link=src["link"],
            date=src["date"],
            unread=src["unread"],
            idx=i,
            draft="",
        )
        if bucket == "needs_action":
            needs_action.append(triaged)
        else:
            worth_reading.append(triaged)
    return needs_action + worth_reading


def select_backend(cfg: Config, environ: Mapping[str, str]) -> tuple[str, CallFn]:
    """Pick which backend answers this run.

    `cfg.provider` other than "auto" is an explicit choice from config.yaml —
    its backend raises its own missing-secret MailError if that secret turns
    out to be absent, same as calling it directly would. "auto" (the default)
    walks PROVIDERS in order and takes the first one whose secret env var is
    set, which is exactly today's ANTHROPIC_API_KEY/CLAUDE_CODE_OAUTH_TOKEN
    precedence — every existing fork keeps working unchanged.
    """
    if cfg.provider != "auto":
        call, _secret = PROVIDERS[cfg.provider]
        return cfg.provider, call
    for name, (call, secret) in PROVIDERS.items():
        if environ.get(secret):
            return name, call
    options = "\n".join(f"  - {name}: set {secret}" for name, (_call, secret) in PROVIDERS.items())
    raise MailError(
        "No AI provider configured — set one of these repo secrets, or set 'provider' in config.yaml "
        f"to pick one explicitly:\n{options}\nSee README.md for where to get credentials for each."
    )


def triage(cfg: Config, emails: list[Email], now: datetime) -> list[Triaged]:
    _name, call = select_backend(cfg, os.environ)
    reply = call(cfg, build_system(cfg), build_user(emails, now), TRIAGE_SCHEMA)
    return pick(cfg, emails, reply)
