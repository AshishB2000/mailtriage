"""config.yaml — the contract between the setup wizard, the engine and the workflow.

The wizard writes this file, the engine reads it, and the committed
``config.yaml`` is the shipped default. A single key-name typo used to mean the
wizard wrote a setting the engine silently ignored, with no error anywhere. The
field names on :class:`Config` are now that contract, and
:meth:`Config.from_mapping` fails loudly on a value it does not accept.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal, get_args

import yaml

from mailtriage.errors import MailError

Delivery = Literal["email", "gmail"]
DELIVERIES: tuple[str, ...] = get_args(Delivery)

# The valid values of Config.provider. Kept here rather than imported from
# mailtriage.triage.PROVIDERS -- config.py must not import triage -- so
# tests/test_providers.py pins this tuple against triage.PROVIDERS' keys
# (plus "auto") instead. Update both together.
Provider = Literal[
    "auto",
    "claude-subscription",
    "claude-api",
    "chatgpt-subscription",
    "openai-api",
    "gemini-api",
    "google-subscription",
]
PROVIDERS: tuple[str, ...] = get_args(Provider)

DraftTone = Literal["friendly", "formal", "casual"]
DRAFT_TONES: tuple[str, ...] = get_args(DraftTone)

# Shared by the global draft_style default and every unset sub-key of a
# per-account draft_style override.
DRAFT_STYLE_DEFAULTS: dict[str, Any] = {"tone": "friendly", "sign_off": "", "language": "auto", "max_sentences": 5}

RULE_KEYS: tuple[str, ...] = ("always_ignore", "always_surface", "always_action")


@dataclass(slots=True)
class Config:
    """Every key the wizard may write. Defaults here are the shipped defaults."""

    delivery: Delivery
    interests: str = ""
    avoid: str = ""
    reading_count: int = 8
    window_hours: int = 13
    subject_prefix: str = "mailtriage"
    email_to: str = ""
    email_from: str = ""
    # "auto" picks the first provider whose secret is set (see
    # mailtriage.triage.PROVIDERS for the order); any other value forces
    # that one backend and lets its own missing-secret error fire instead.
    provider: str = "auto"
    # Overrides each backend's own MODEL constant when non-empty.
    model: str = ""
    # AI drafts a reply for every needs_action email -- into the digest and
    # your Gmail drafts folder; never sends.
    draft_replies: bool = True
    # tone/sign_off/language/max_sentences for AI-drafted replies. Partial
    # mappings merge over DRAFT_STYLE_DEFAULTS.
    draft_style: dict[str, Any] = field(default_factory=lambda: dict(DRAFT_STYLE_DEFAULTS))
    # Hard VIP-sender rules, checked deterministically -- see rules.py.
    rules: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in RULE_KEYS})
    # Per-account overrides keyed by lowercased address: interests/avoid
    # (added to the global ones) and/or draft_style (merged over the global
    # draft_style). See rules.py / triage/__init__.py / drafts.py.
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict[str, Any], origin: str = "config.yaml") -> Config:
        known = {f.name for f in fields(cls)}

        # An unknown key is almost always a typo in a hand-edited file or a
        # wizard/engine version skew. Warn, never fail: a fork that adds a key
        # for its own tooling should still get its digest.
        for key in sorted(set(data) - known):
            print(f"mailtriage: ignoring unknown key {key!r} in {origin}", file=sys.stderr)

        delivery = data.get("delivery")
        if delivery not in DELIVERIES:
            raise MailError(f"'delivery' in {origin} must be one of {DELIVERIES} (got {delivery!r}).")

        cfg = cls(delivery=delivery)
        for name in known - {"delivery"}:
            if name in data and data[name] is not None:
                setattr(cfg, name, data[name])

        for name in ("reading_count", "window_hours"):
            value = getattr(cfg, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise MailError(f"'{name}' in {origin} must be a positive whole number (got {value!r}).")

        if not isinstance(cfg.draft_replies, bool):
            raise MailError(f"'draft_replies' in {origin} must be true or false (got {cfg.draft_replies!r}).")

        # str() rather than a type error: YAML turns a bare value into whatever
        # type it looks like (e.g. an unquoted prefix or address).
        for name in ("interests", "avoid", "subject_prefix", "email_to", "email_from", "provider", "model"):
            setattr(cfg, name, str(getattr(cfg, name)))

        if cfg.provider not in PROVIDERS:
            raise MailError(f"'provider' in {origin} must be one of {PROVIDERS} (got {cfg.provider!r}).")

        cfg.draft_style = _validate_draft_style(cfg.draft_style, DRAFT_STYLE_DEFAULTS, origin, "draft_style")
        cfg.rules = _validate_rules(cfg.rules, origin)
        cfg.accounts = _validate_accounts(cfg.accounts, cfg.draft_style, origin)

        return cfg


def _validate_draft_style(data: Any, base: dict[str, Any], origin: str, where: str) -> dict[str, Any]:
    """Merge a (possibly partial) draft_style mapping over `base` and validate
    it. `base` is DRAFT_STYLE_DEFAULTS for the global setting, or the already-
    validated global draft_style for a per-account override."""
    if not isinstance(data, dict):
        raise MailError(f"'{where}' in {origin} must be a mapping (got {type(data).__name__}).")
    known = {"tone", "sign_off", "language", "max_sentences"}
    for key in sorted(set(data) - known):
        print(f"mailtriage: ignoring unknown key {key!r} in {where} in {origin}", file=sys.stderr)

    style = {**base, **{k: v for k, v in data.items() if k in known}}

    if style["tone"] not in DRAFT_TONES:
        raise MailError(f"'{where}.tone' in {origin} must be one of {DRAFT_TONES} (got {style['tone']!r}).")
    style["sign_off"] = str(style["sign_off"])
    style["language"] = str(style["language"])
    max_sentences = style["max_sentences"]
    if not isinstance(max_sentences, int) or isinstance(max_sentences, bool) or max_sentences < 1:
        raise MailError(f"'{where}.max_sentences' in {origin} must be a positive whole number (got {max_sentences!r}).")
    return style


def _validate_rules(data: Any, origin: str) -> dict[str, list[str]]:
    if not isinstance(data, dict):
        raise MailError(f"'rules' in {origin} must be a mapping (got {type(data).__name__}).")
    for key in sorted(set(data) - set(RULE_KEYS)):
        print(f"mailtriage: ignoring unknown key {key!r} in rules in {origin}", file=sys.stderr)

    out: dict[str, list[str]] = {k: [] for k in RULE_KEYS}
    for k in RULE_KEYS:
        if k not in data:
            continue
        entries = data[k]
        if not isinstance(entries, list) or not all(isinstance(e, str) and e for e in entries):
            raise MailError(f"'rules.{k}' in {origin} must be a list of non-empty strings (got {entries!r}).")
        out[k] = entries
    return out


def _validate_accounts(data: Any, global_style: dict[str, Any], origin: str) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict):
        raise MailError(f"'accounts' in {origin} must be a mapping (got {type(data).__name__}).")
    known = {"interests", "avoid", "draft_style"}
    out: dict[str, dict[str, Any]] = {}
    for addr, val in data.items():
        if not isinstance(val, dict):
            raise MailError(f"'accounts.{addr}' in {origin} must be a mapping (got {type(val).__name__}).")
        for key in sorted(set(val) - known):
            print(f"mailtriage: ignoring unknown key {key!r} in accounts.{addr} in {origin}", file=sys.stderr)

        entry: dict[str, Any] = {}
        if "interests" in val:
            entry["interests"] = str(val["interests"])
        if "avoid" in val:
            entry["avoid"] = str(val["avoid"])
        if "draft_style" in val:
            entry["draft_style"] = _validate_draft_style(
                val["draft_style"], global_style, origin, f"accounts.{addr}.draft_style"
            )
        out[str(addr).lower()] = entry
    return out


def load_config(path: str | Path, environ: Mapping[str, str] | None = None) -> Config:
    path = Path(path)
    if not path.exists():
        raise MailError(f"{path} not found. Run the setup wizard, or copy config.yaml from the repo root.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise MailError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise MailError(f"{path} must be a YAML mapping of settings, not {type(data).__name__}.")
    cfg = Config.from_mapping(data, origin=str(path))

    # Addresses are private even though config.yaml is public (the repo it
    # ships in is a fork someone else can read). EMAIL_TO/EMAIL_FROM secrets
    # win over whatever config.yaml says, blank or not.
    environ = os.environ if environ is None else environ
    if environ.get("EMAIL_TO"):
        cfg.email_to = environ["EMAIL_TO"]
    if environ.get("EMAIL_FROM"):
        cfg.email_from = environ["EMAIL_FROM"]
    return cfg
