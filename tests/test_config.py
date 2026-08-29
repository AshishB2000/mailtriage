"""config.yaml is the contract between the wizard, the engine and the workflow.

These tests exist to make a key-name drift fail loudly instead of silently
producing a triage that ignores half the user's settings.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mailtriage.config import Config, load_config
from mailtriage.errors import MailError

MINIMAL = {"delivery": "email"}


def test_defaults_match_shipped_config():
    """The dataclass defaults must equal the committed config.yaml, or a fork that
    deletes a key silently gets different behavior from one that keeps it."""
    cfg = Config.from_mapping(MINIMAL)
    assert cfg.reading_count == 8
    assert cfg.window_hours == 13
    assert cfg.subject_prefix == "mailtriage"


def test_shipped_config_yaml_loads():
    """The committed config.yaml must parse — the wizard writes this shape."""
    cfg = load_config("config.yaml")
    # Don't pin the shipped delivery choice — it's user-editable; just require a valid one.
    assert cfg.delivery in ("email", "gmail")
    assert cfg.window_hours == 13


def test_shipped_config_yaml_is_loadable_via_from_mapping():
    shipped = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    Config.from_mapping(shipped)  # must not raise


def test_unknown_key_warns_but_does_not_fail(capsys):
    cfg = Config.from_mapping({"delivery": "email", "made_up_key": 1})
    assert cfg.delivery == "email"
    assert "made_up_key" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ({}, "delivery"),
        ({"delivery": "telegram"}, "delivery"),
        ({"delivery": None}, "delivery"),
        ({**MINIMAL, "reading_count": 0}, "reading_count"),
        ({**MINIMAL, "reading_count": "eight"}, "reading_count"),
        ({**MINIMAL, "window_hours": -1}, "window_hours"),
        # bool is an int subclass — must not sneak through the positive-int check
        ({**MINIMAL, "reading_count": True}, "reading_count"),
    ],
)
def test_invalid_config_raises_with_a_useful_message(bad, fragment):
    with pytest.raises(MailError) as e:
        Config.from_mapping(bad)
    assert fragment in str(e.value)


def test_yaml_coerced_types_become_strings():
    """YAML turns an unquoted value into a non-str type; the delivery code needs a string."""
    cfg = Config.from_mapping({**MINIMAL, "email_to": 123456})
    assert cfg.email_to == "123456"


def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(MailError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_malformed_yaml_is_reported_as_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("delivery: [unclosed", encoding="utf-8")
    with pytest.raises(MailError, match="not valid YAML"):
        load_config(p)


def test_env_overlay_wins_over_yaml(tmp_path):
    """Addresses are private even on a public fork -- EMAIL_TO/EMAIL_FROM secrets
    must override whatever config.yaml says, so a public config.yaml never has to
    carry a real address."""
    p = tmp_path / "config.yaml"
    p.write_text(
        'delivery: email\nemail_to: "yaml@example.com"\nemail_from: "yaml-from@example.com"\n', encoding="utf-8"
    )
    cfg = load_config(p, environ={"EMAIL_TO": "env@example.com", "EMAIL_FROM": "env-from@example.com"})
    assert cfg.email_to == "env@example.com"
    assert cfg.email_from == "env-from@example.com"


def test_env_overlay_leaves_blank_when_unset(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("delivery: gmail\n", encoding="utf-8")
    cfg = load_config(p, environ={})
    assert cfg.email_to == ""
    assert cfg.email_from == ""
