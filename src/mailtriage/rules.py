"""Apply the reader's hard rules -- VIP senders enforced deterministically,
before the model ever sees the inbox (always_ignore) and after it has picked
(always_surface / always_action). No API call, no state.

Precedence when one address matches both always_ignore and always_action:
ACTION WINS. Ignoring is the reader's general "don't bother me" setting;
listing the same address under always_action is a more specific, later
decision that this one sender's messages must never be silenced. See
test_rules.py for the case this docstring promises.
"""

from __future__ import annotations

from email.utils import parseaddr

from mailtriage.config import Config
from mailtriage.models import Email, Triaged


def matches(entry: str, from_header: str) -> bool:
    """`entry` is a full address ("boss@corp.com") or a domain rule
    ("@corp.com"). Matching is case-insensitive, against the address part of
    `from_header` (display names like '"Boss" <boss@corp.com>' are stripped
    via email.utils.parseaddr). A domain rule matches that domain and its
    subdomains -- "@corp.com" matches "x@mail.corp.com" -- but never a mere
    suffix: "@corp.com" must NOT match "x@notcorp.com".
    """
    _, addr = parseaddr(from_header)
    addr = addr.lower()
    entry = entry.strip().lower()
    if entry.startswith("@"):
        domain = entry[1:]
        addr_domain = addr.rsplit("@", 1)[-1]
        return addr_domain == domain or addr_domain.endswith("." + domain)
    return addr == entry


def apply_ignore(cfg: Config, emails: list[Email]) -> list[Email]:
    """Drop always_ignore matches before the model ever sees them. An address
    that also matches always_action is kept -- action wins, see module
    docstring. Callers wanting a dropped count can compare len(emails) before
    and after (library code never prints)."""
    ignore = cfg.rules.get("always_ignore", [])
    if not ignore:
        return emails
    action = cfg.rules.get("always_action", [])
    return [
        em
        for em in emails
        if not any(matches(e, em["from"]) for e in ignore) or any(matches(e, em["from"]) for e in action)
    ]


def _forced(em: Email, i: int, bucket: str, note: str) -> Triaged:
    return Triaged(
        bucket=bucket,
        note=note,
        account=em["account"],
        sender=em["from"],
        subject=em["subject"],
        link=em["link"],
        date=em["date"],
        unread=em["unread"],
        idx=i,
        draft="",
    )


def enforce(cfg: Config, emails: list[Email], kept: list[Triaged]) -> list[Triaged]:
    """Apply always_action / always_surface after pick(). Keeps pick()'s
    ordering contract: needs_action first, then worth_reading. Deduplicates
    by idx -- each email appears at most once in the result."""
    by_idx: dict[int, Triaged] = {t["idx"]: t for t in kept}

    always_action = cfg.rules.get("always_action", [])
    if always_action:
        for i, em in enumerate(emails):
            if not any(matches(e, em["from"]) for e in always_action):
                continue
            existing = by_idx.get(i)
            if existing is not None and existing["bucket"] == "needs_action":
                continue  # model already got it right
            note = existing["note"] if existing is not None else f"rule: always action from {em['from']}"
            by_idx[i] = _forced(em, i, "needs_action", note)

    always_surface = cfg.rules.get("always_surface", [])
    if always_surface:
        for i, em in enumerate(emails):
            if i in by_idx:
                continue  # already surfaced somehow -- always_surface only adds absentees
            if not any(matches(e, em["from"]) for e in always_surface):
                continue
            by_idx[i] = _forced(em, i, "worth_reading", f"rule: always surface from {em['from']}")

    needs_action = [t for t in by_idx.values() if t["bucket"] == "needs_action"]
    worth_reading = [t for t in by_idx.values() if t["bucket"] == "worth_reading"]
    return needs_action + worth_reading
