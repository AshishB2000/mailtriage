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


# --- draft_style -------------------------------------------------------------


def test_draft_style_defaults():
    cfg = Config.from_mapping(MINIMAL)
    assert cfg.draft_style == {"tone": "friendly", "sign_off": "", "language": "auto", "max_sentences": 5}


def test_draft_style_partial_mapping_merges_over_defaults():
    cfg = Config.from_mapping({**MINIMAL, "draft_style": {"tone": "formal"}})
    assert cfg.draft_style == {"tone": "formal", "sign_off": "", "language": "auto", "max_sentences": 5}


def test_draft_style_must_be_a_mapping():
    with pytest.raises(MailError, match="draft_style"):
        Config.from_mapping({**MINIMAL, "draft_style": "formal"})


def test_draft_style_unknown_subkey_warns(capsys):
    Config.from_mapping({**MINIMAL, "draft_style": {"tone": "formal", "made_up": 1}})
    assert "made_up" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ({"tone": "sarcastic"}, "tone"),
        ({"max_sentences": 0}, "max_sentences"),
        ({"max_sentences": "five"}, "max_sentences"),
        ({"max_sentences": True}, "max_sentences"),  # bool is an int subclass -- must not sneak through
    ],
)
def test_draft_style_invalid_raises(bad, fragment):
    with pytest.raises(MailError) as e:
        Config.from_mapping({**MINIMAL, "draft_style": bad})
    assert fragment in str(e.value)


def test_draft_style_sign_off_and_language_coerced_to_str():
    cfg = Config.from_mapping({**MINIMAL, "draft_style": {"sign_off": 5, "language": 7}})
    assert cfg.draft_style["sign_off"] == "5"
    assert cfg.draft_style["language"] == "7"


# --- rules ---------------------------------------------------------------


def test_rules_defaults():
    cfg = Config.from_mapping(MINIMAL)
    assert cfg.rules == {"always_ignore": [], "always_surface": [], "always_action": []}


def test_rules_partial_mapping():
    cfg = Config.from_mapping({**MINIMAL, "rules": {"always_ignore": ["a@b.com"]}})
    assert cfg.rules == {"always_ignore": ["a@b.com"], "always_surface": [], "always_action": []}


def test_rules_must_be_a_mapping():
    with pytest.raises(MailError, match="rules"):
        Config.from_mapping({**MINIMAL, "rules": ["a@b.com"]})


def test_rules_value_must_be_list_of_nonempty_strings():
    with pytest.raises(MailError, match="always_ignore"):
        Config.from_mapping({**MINIMAL, "rules": {"always_ignore": "a@b.com"}})
    with pytest.raises(MailError, match="always_ignore"):
        Config.from_mapping({**MINIMAL, "rules": {"always_ignore": [""]}})
    with pytest.raises(MailError, match="always_ignore"):
        Config.from_mapping({**MINIMAL, "rules": {"always_ignore": [1]}})


def test_rules_unknown_subkey_warns(capsys):
    Config.from_mapping({**MINIMAL, "rules": {"made_up": ["x"]}})
    assert "made_up" in capsys.readouterr().err


# --- accounts --------------------------------------------------------------


def test_accounts_default_empty():
    cfg = Config.from_mapping(MINIMAL)
    assert cfg.accounts == {}


def test_accounts_keys_lowercased():
    cfg = Config.from_mapping({**MINIMAL, "accounts": {"Work@Corp.com": {"interests": "eng leads"}}})
    assert "work@corp.com" in cfg.accounts
    assert cfg.accounts["work@corp.com"]["interests"] == "eng leads"


def test_accounts_must_be_a_mapping():
    with pytest.raises(MailError, match="accounts"):
        Config.from_mapping({**MINIMAL, "accounts": ["work@corp.com"]})


def test_accounts_entry_must_be_a_mapping():
    with pytest.raises(MailError, match="accounts"):
        Config.from_mapping({**MINIMAL, "accounts": {"work@corp.com": "eng leads"}})


def test_accounts_unknown_subkey_warns(capsys):
    Config.from_mapping({**MINIMAL, "accounts": {"work@corp.com": {"made_up": 1}}})
    assert "made_up" in capsys.readouterr().err


def test_accounts_draft_style_merges_over_the_global_draft_style_not_defaults():
    cfg = Config.from_mapping(
        {
            **MINIMAL,
            "draft_style": {"tone": "formal", "max_sentences": 3},
            "accounts": {"work@corp.com": {"draft_style": {"sign_off": "Best, Alex"}}},
        }
    )
    # the account only overrode sign_off -- tone/max_sentences must come from
    # the already-customized GLOBAL draft_style, not the bare defaults.
    assert cfg.accounts["work@corp.com"]["draft_style"] == {
        "tone": "formal",
        "sign_off": "Best, Alex",
        "language": "auto",
        "max_sentences": 3,
    }


def test_accounts_draft_style_invalid_raises():
    with pytest.raises(MailError, match="tone"):
        Config.from_mapping({**MINIMAL, "accounts": {"work@corp.com": {"draft_style": {"tone": "bogus"}}}})
