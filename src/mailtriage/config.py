"""config.yaml — the contract between the setup wizard, the engine and the workflow.

The wizard writes this file, the engine reads it, and the committed
``config.yaml`` is the shipped default. A single key-name typo used to mean the
wizard wrote a setting the engine silently ignored, with no error anywhere. The
field names on :class:`Config` are now that contract, and
:meth:`Config.from_mapping` fails loudly on a value it does not accept.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Literal, get_args

import yaml

from mailtriage.errors import MailError

Delivery = Literal["email", "gmail"]
DELIVERIES: tuple[str, ...] = get_args(Delivery)


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

        # str() rather than a type error: YAML turns a bare value into whatever
        # type it looks like (e.g. an unquoted prefix or address).
        for name in ("interests", "avoid", "subject_prefix", "email_to", "email_from"):
            setattr(cfg, name, str(getattr(cfg, name)))

        return cfg


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise MailError(f"{path} not found. Run the setup wizard, or copy config.yaml from the repo root.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise MailError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise MailError(f"{path} must be a YAML mapping of settings, not {type(data).__name__}.")
    return Config.from_mapping(data, origin=str(path))
