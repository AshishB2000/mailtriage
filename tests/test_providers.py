"""select_backend() is the auto-detection logic every fork relies on for its
existing secret(s) to keep working unchanged after this five-provider split.
"""

from __future__ import annotations

import pytest

from mailtriage import config as config_module
from mailtriage.config import Config
from mailtriage.errors import MailError
from mailtriage.triage import PROVIDERS, claude_api, claude_cli, codex_cli, gemini_api, openai_api, select_backend

CFG_AUTO = Config(delivery="email")


def test_config_provider_choices_mirror_triage_providers():
    """config.py can't import triage (the contract forbids it), so its valid-
    provider tuple is a hand-maintained mirror of triage.PROVIDERS' keys, plus
    "auto". This pins the two together so they can't silently drift apart."""
    assert set(config_module.PROVIDERS) == {"auto", *PROVIDERS.keys()}


@pytest.mark.parametrize(
    "secret, expected_name, expected_call",
    [
        ("CLAUDE_CODE_OAUTH_TOKEN", "claude-subscription", claude_cli.call),
        ("ANTHROPIC_API_KEY", "claude-api", claude_api.call),
        ("CODEX_AUTH_JSON", "chatgpt-subscription", codex_cli.call),
        ("OPENAI_API_KEY", "openai-api", openai_api.call),
        ("GEMINI_API_KEY", "gemini-api", gemini_api.call),
    ],
)
def test_auto_mode_each_secret_alone_selects_its_backend(secret, expected_name, expected_call):
    name, call = select_backend(CFG_AUTO, {secret: "set"})
    assert name == expected_name
    assert call is expected_call


def test_auto_mode_precedence_when_multiple_secrets_set():
    # PROVIDERS dict order is the precedence order -- this is today's
    # CLAUDE_CODE_OAUTH_TOKEN-over-ANTHROPIC_API_KEY behavior, extended.
    environ = {
        "GEMINI_API_KEY": "g",
        "OPENAI_API_KEY": "o",
        "CODEX_AUTH_JSON": "c",
        "ANTHROPIC_API_KEY": "a",
        "CLAUDE_CODE_OAUTH_TOKEN": "t",
    }
    name, _call = select_backend(CFG_AUTO, environ)
    assert name == "claude-subscription"

    name, _call = select_backend(CFG_AUTO, {k: v for k, v in environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"})
    assert name == "claude-api"

    name, _call = select_backend(
        CFG_AUTO, {k: v for k, v in environ.items() if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")}
    )
    assert name == "chatgpt-subscription"


def test_explicit_provider_overrides_auto_order():
    cfg = Config(delivery="email", provider="gemini-api")
    # Even with every other secret set, an explicit provider wins.
    environ = {"CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "a"}
    name, call = select_backend(cfg, environ)
    assert name == "gemini-api"
    assert call is gemini_api.call


def test_no_secrets_set_raises_and_lists_every_option():
    with pytest.raises(MailError) as exc_info:
        select_backend(CFG_AUTO, {})
    message = str(exc_info.value)
    for name, (_call, secret) in PROVIDERS.items():
        assert name in message
        assert secret in message


def test_unknown_provider_in_config_raises_from_validation():
    with pytest.raises(MailError, match="provider"):
        Config.from_mapping({"delivery": "email", "provider": "not-a-real-provider"})
