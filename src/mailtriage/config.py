"""config.yaml — the contract between the setup wizard, the engine and the workflow.

The wizard writes this file, the engine reads it, and the committed
``config.yaml`` is the shipped default. A single key-name typo used to mean the
wizard wrote a setting the engine silently ignored, with no error anywhere. The
field names on :class:`Config` are now that contract, and
:meth:`Config.from_mapping` fails loudly on a value it does not accept.
"""

from __future__ import annotations

import copy
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfoNotFoundError

import yaml

from mailtriage.errors import MailError

_RUN_AT_RE = re.compile(r"^\d{2}:\d{2}$")
_WEEKLY_RE = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun) (\d{2}:\d{2})$", re.IGNORECASE)


def _valid_time(hhmm: str) -> bool:
    h, m = hhmm.split(":")
    return 0 <= int(h) <= 23 and 0 <= int(m) <= 59


Delivery = Literal["email", "gmail", "telegram", "slack", "discord", "ntfy"]
DELIVERIES: tuple[str, ...] = get_args(Delivery)
# The two email deliveries; the chat channels are text-only by nature.
EMAIL_DELIVERIES: tuple[str, ...] = ("email", "gmail")

DigestFormat = Literal["html", "text"]
DIGEST_FORMATS: tuple[str, ...] = get_args(DigestFormat)

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
DRAFT_STYLE_DEFAULTS: dict[str, Any] = {
    "tone": "friendly",
    "sign_off": "",
    "language": "auto",
    "max_sentences": 5,
    # Show the drafting model up to 3 of the reader's own recent Sent
    # messages to the same recipient (or domain) so drafts sound like them.
    "learn_voice": True,
}

RULE_KEYS: tuple[str, ...] = ("always_ignore", "always_surface", "always_action")

NOISE_DEFAULTS: dict[str, bool] = {"label": False, "archive": False}


@dataclass(slots=True)
class Config:
    """Every key the wizard may write. Defaults here are the shipped defaults."""

    delivery: Delivery
    interests: str = ""
    avoid: str = ""
    reading_count: int = 8
    # 15 = the default run_at slots' 14h overnight gap + 1h slack -- see the
    # gap-vs-window_hours warning below.
    window_hours: int = 15
    # Daily digest times, "HH:MM" 24h, in `timezone`. GitHub Actions runs the
    # workflow hourly and schedule.due() decides which hour is one of these.
    run_at: list[str] = field(default_factory=lambda: ["08:00", "18:00"])
    # IANA name (e.g. "America/New_York") that `run_at` and `weekly_review`
    # are interpreted in. See the tz database list linked in the error this
    # raises for a bad name.
    timezone: str = "UTC"
    # Blank = off. Otherwise "<mon|tue|wed|thu|fri|sat|sun> HH:MM" -- a weekly
    # slot on top of the daily ones. The weekly digest itself ships in a
    # later PR; for now schedule.due() only recognizes the slot.
    weekly_review: str = ""
    # How long after a run_at/weekly_review slot the hourly gate still fires
    # it. GitHub's cron skips hours under load; 120 = one skipped hour still
    # gets its digest. The slot-stamped subject guard (imap_pull.
    # already_delivered) keeps a wide window from sending a slot twice.
    catch_up_minutes: int = 120
    subject_prefix: str = "mailtriage"
    email_to: str = ""
    email_from: str = ""
    # delivery: telegram only. The numeric chat id the bot posts to -- see
    # README "Delivery options" for how to read it off getUpdates.
    telegram_chat_id: str = ""
    # delivery: email/gmail only. "text" sends a plain-text digest (the same
    # rendering the chat channels get) instead of the HTML one.
    digest_format: str = "html"
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
    # 1 = one draft per needs_action item. 2 = a short and a full variant,
    # both pushed to Gmail Drafts; the digest shows the short one.
    draft_variants: int = 1
    # Hard VIP-sender rules, checked deterministically -- see rules.py.
    rules: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in RULE_KEYS})
    # Per-account overrides keyed by lowercased address: interests/avoid
    # (added to the global ones) and/or draft_style (merged over the global
    # draft_style). See rules.py / triage/__init__.py / drafts.py.
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Re-list a needs_action email in every digest until it's handled --
    # cleared by replying, archiving, or removing the Gmail label below. The
    # label itself is the memory; no state is kept in this repo.
    carry_over: bool = True
    # Gmail label the engine applies to needs_action mail and searches for on
    # the next run. A "/" nests it under a parent in Gmail's sidebar (e.g.
    # "mailtriage/action" shows as mailtriage -> action) -- intended.
    label: str = "mailtriage/action"
    # A carried item open for at least this many days is flagged "still open"
    # (bold row + badge) in the digest's "Still waiting on you" section.
    nag_after_days: int = 3
    # Put today's calendar at the top of the digest. Only does anything when
    # the CALENDAR_ICS_URL secret (a private ICS feed URL) is set.
    calendar: bool = True
    # Open the weekly review with a model-written paragraph (one extra call
    # a week). A failed call falls back to the plain review, never no review.
    weekly_narrative: bool = True
    # BCP-47 base code for the digest's own words -- section headings,
    # badges, footers. NOT what the model writes in: that is
    # draft_style.language. Unknown code warns and falls back to "en".
    # See delivery/strings.py for the languages that have a table.
    language: str = "en"
    # Show the model up to 2 earlier messages of a candidate's Gmail thread
    # (read from All Mail, newest 15 candidates per run) so it can tell a
    # live conversation from a stale one. Read-only, a few extra fetches.
    thread_context: bool = True
    # Count how often the reader has written to each candidate's sender in
    # the last 180 days (\Sent, newest 40 senders per run) and tell the model.
    sender_memory: bool = True
    # Folded "Noise this week" footer in the digest: one Unsubscribe link per
    # omitted sender that offered one (https or mailto only). Never clicked
    # for you.
    show_unsubscribe: bool = True
    # Opt-in, both off. label: apply "mailtriage/noise" to the candidates a
    # run left out (never a sender your always_surface/always_action rules
    # name). archive (requires label): also take them out of the inbox by
    # removing the \Inbox label -- they stay in All Mail, searchable, never
    # deleted. Skipped on --dry-run.
    noise: dict[str, bool] = field(default_factory=lambda: dict(NOISE_DEFAULTS))

    # Named digests, each over a subset of MAIL_ACCOUNTS with its own
    # overrides for any key above (delivery, run_at, interests, ...). Empty =
    # one digest over every account, i.e. everything above as-is. See
    # `profile()` and README "Two digests: work and personal".
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)

    def profile(self, name: str) -> Config:
        """This config with `profiles[name]`'s overrides applied, validated
        the same way the top level is. `subject_prefix` defaults to
        "<base prefix> · <name>" so two digests are told apart at a glance.
        The result has no profiles of its own."""
        spec = self.profiles[name]
        base = {f.name: copy.deepcopy(getattr(self, f.name)) for f in fields(self) if f.name != "profiles"}
        base["subject_prefix"] = f"{self.subject_prefix} · {name}"
        overrides = {k: v for k, v in spec.items() if k != "accounts"}
        return Config.from_mapping({**base, **overrides}, origin=f"profiles.{name}")

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

        for name in ("reading_count", "window_hours", "nag_after_days"):
            value = getattr(cfg, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise MailError(f"'{name}' in {origin} must be a positive whole number (got {value!r}).")

        cu = cfg.catch_up_minutes
        if not isinstance(cu, int) or isinstance(cu, bool) or not 60 <= cu <= 360:
            raise MailError(f"'catch_up_minutes' in {origin} must be a whole number from 60 to 360 (got {cu!r}).")

        for name in (
            "draft_replies",
            "carry_over",
            "thread_context",
            "sender_memory",
            "show_unsubscribe",
            "calendar",
            "weekly_narrative",
        ):
            value = getattr(cfg, name)
            if not isinstance(value, bool):
                raise MailError(f"'{name}' in {origin} must be true or false (got {value!r}).")

        if cfg.draft_variants not in (1, 2) or isinstance(cfg.draft_variants, bool):
            raise MailError(f"'draft_variants' in {origin} must be 1 or 2 (got {cfg.draft_variants!r}).")

        # str() rather than a type error: YAML turns a bare value into whatever
        # type it looks like (e.g. an unquoted prefix or address).
        for name in (
            "interests",
            "avoid",
            "subject_prefix",
            "email_to",
            "email_from",
            "provider",
            "model",
            "timezone",
            "weekly_review",
            "label",
            "language",
            "telegram_chat_id",
            "digest_format",
        ):
            setattr(cfg, name, str(getattr(cfg, name)))

        if not cfg.label.strip():
            raise MailError(f"'label' in {origin} must not be empty.")

        if cfg.digest_format not in DIGEST_FORMATS:
            raise MailError(f"'digest_format' in {origin} must be one of {DIGEST_FORMATS} (got {cfg.digest_format!r}).")

        if cfg.provider not in PROVIDERS:
            raise MailError(f"'provider' in {origin} must be one of {PROVIDERS} (got {cfg.provider!r}).")

        cfg.draft_style = _validate_draft_style(cfg.draft_style, DRAFT_STYLE_DEFAULTS, origin, "draft_style")
        cfg.rules = _validate_rules(cfg.rules, origin)
        cfg.noise = _validate_noise(cfg.noise, origin)
        cfg.accounts = _validate_accounts(cfg.accounts, cfg.draft_style, origin)
        cfg.profiles = _validate_profiles(cfg.profiles, known, origin)
        if not isinstance(cfg.run_at, list) or not cfg.run_at:
            raise MailError(f"'run_at' in {origin} must be a non-empty list of \"HH:MM\" times (got {cfg.run_at!r}).")
        deduped: list[str] = []
        for slot in cfg.run_at:
            if not isinstance(slot, str) or not _RUN_AT_RE.match(slot) or not _valid_time(slot):
                raise MailError(f"'run_at' entry {slot!r} in {origin} must look like \"HH:MM\" (24h, zero-padded).")
            if slot not in deduped:
                deduped.append(slot)
        cfg.run_at = deduped

        # Deferred import: schedule.py imports Config from this module for its
        # own type hints, so importing it at config.py's top level would be a
        # circular import. Calling it from inside a function (after config.py
        # has already finished loading) sidesteps that -- config.py stays
        # import-light at module scope either way.
        # `known` would shadow the local set of field names above.
        from mailtriage.delivery.strings import LANGUAGES
        from mailtriage.delivery.strings import known as known_language
        from mailtriage.schedule import local_zone, max_gap_pair

        # A warning, not an error: an unknown language still gets a digest,
        # in English. Failing the run over the wording of a heading would be
        # a worse trade than an English heading.
        if not known_language(cfg.language):
            print(
                f"mailtriage: no translation for language {cfg.language!r} in {origin}; "
                f"using English. Available: {', '.join(LANGUAGES)}.",
                file=sys.stderr,
            )

        try:
            local_zone(cfg.timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise MailError(
                f"'timezone' in {origin} must be a valid IANA time zone name (got {cfg.timezone!r}). "
                "See https://en.wikipedia.org/wiki/List_of_tz_database_time_zones."
            ) from e

        if cfg.weekly_review:
            m = _WEEKLY_RE.match(cfg.weekly_review.strip())
            if not m or not _valid_time(m.group(2)):
                raise MailError(
                    f"'weekly_review' in {origin} must be blank or "
                    f'"<mon|tue|wed|thu|fri|sat|sun> HH:MM" (got {cfg.weekly_review!r}).'
                )
            cfg.weekly_review = f"{m.group(1).lower()} {m.group(2)}"

        # A warning, not an error: the run still works, it just misses mail
        # published inside the uncovered gap. Strict "<", not "<=" -- a
        # window_hours exactly equal to the gap leaves zero slack for
        # scheduler drift, which is still a real risk, hence "+ 1".
        gap, slot_a, slot_b = max_gap_pair(cfg.run_at)
        if cfg.window_hours < gap + 1:
            print(
                f"mailtriage: window_hours={cfg.window_hours} is smaller than the {gap:g}h gap between runs "
                f"at {slot_a} and {slot_b} (+1h slack); mail arriving in the gap will be missed. "
                f"Set window_hours to at least {gap + 1:g}.",
                file=sys.stderr,
            )

        return cfg


def _validate_draft_style(data: Any, base: dict[str, Any], origin: str, where: str) -> dict[str, Any]:
    """Merge a (possibly partial) draft_style mapping over `base` and validate
    it. `base` is DRAFT_STYLE_DEFAULTS for the global setting, or the already-
    validated global draft_style for a per-account override."""
    if not isinstance(data, dict):
        raise MailError(f"'{where}' in {origin} must be a mapping (got {type(data).__name__}).")
    known = {"tone", "sign_off", "language", "max_sentences", "learn_voice"}
    for key in sorted(set(data) - known):
        print(f"mailtriage: ignoring unknown key {key!r} in {where} in {origin}", file=sys.stderr)

    style = {**base, **{k: v for k, v in data.items() if k in known}}

    if style["tone"] not in DRAFT_TONES:
        raise MailError(f"'{where}.tone' in {origin} must be one of {DRAFT_TONES} (got {style['tone']!r}).")
    if not isinstance(style["learn_voice"], bool):
        raise MailError(f"'{where}.learn_voice' in {origin} must be true or false (got {style['learn_voice']!r}).")
    style["sign_off"] = str(style["sign_off"])
    style["language"] = str(style["language"])
    max_sentences = style["max_sentences"]
    if not isinstance(max_sentences, int) or isinstance(max_sentences, bool) or max_sentences < 1:
        raise MailError(f"'{where}.max_sentences' in {origin} must be a positive whole number (got {max_sentences!r}).")
    return style


def _validate_noise(data: Any, origin: str) -> dict[str, bool]:
    if not isinstance(data, dict):
        raise MailError(f"'noise' in {origin} must be a mapping (got {type(data).__name__}).")
    for key in sorted(set(data) - set(NOISE_DEFAULTS)):
        print(f"mailtriage: ignoring unknown key {key!r} in noise in {origin}", file=sys.stderr)
    out = {**NOISE_DEFAULTS, **{k: v for k, v in data.items() if k in NOISE_DEFAULTS}}
    for k, v in out.items():
        if not isinstance(v, bool):
            raise MailError(f"'noise.{k}' in {origin} must be true or false (got {v!r}).")
    if out["archive"] and not out["label"]:
        raise MailError(
            f"'noise.archive' in {origin} requires 'noise.label: true' -- archived mail must stay findable."
        )
    return out


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


def _validate_profiles(data: Any, known: set[str], origin: str) -> dict[str, dict[str, Any]]:
    """Shape only: each profile is a mapping with a non-empty `accounts` list
    plus overrides for known top-level keys (unknown ones warn, like the top
    level). The override VALUES are validated when `Config.profile()`
    resolves the profile -- the same from_mapping rules, no second copy."""
    if not isinstance(data, dict):
        raise MailError(f"'profiles' in {origin} must be a mapping of profile name -> settings.")
    out: dict[str, dict[str, Any]] = {}
    for name, spec in data.items():
        where = f"profiles.{name}"
        if not isinstance(spec, dict):
            raise MailError(f"'{where}' in {origin} must be a mapping (got {type(spec).__name__}).")
        accounts = spec.get("accounts")
        if (
            not isinstance(accounts, list)
            or not accounts
            or not all(isinstance(a, str) and a.strip() for a in accounts)
        ):
            raise MailError(
                f"'{where}.accounts' in {origin} must be a non-empty list of addresses from MAIL_ACCOUNTS "
                f"(got {accounts!r})."
            )
        # In a profile, `accounts` is the address list (not the top level's
        # per-account overrides map) and `profiles` can't nest.
        allowed = (known - {"profiles"}) | {"accounts"}
        for key in sorted(set(spec) - allowed):
            print(f"mailtriage: ignoring unknown key {key!r} in {where} in {origin}", file=sys.stderr)
        out[str(name)] = {k: v for k, v in spec.items() if k in allowed}
        out[str(name)]["accounts"] = [a.strip().lower() for a in accounts]
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
