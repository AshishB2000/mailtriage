"""Claude via the Anthropic API (ANTHROPIC_API_KEY).

HARD CONSTRAINT: this module must not import `anthropic` at module scope —
selfcheck.py's import chain must work with the SDK uninstalled. The import
lives inside call() only.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from mailtriage.config import Config
from mailtriage.errors import MailError
from mailtriage.triage.usage import log_usage

if TYPE_CHECKING:  # anthropic's own types, for annotations only — never imported at runtime
    from anthropic.types.message_param import MessageParam
    from anthropic.types.tool_choice_tool_param import ToolChoiceToolParam
    from anthropic.types.tool_param import ToolParam

# Headline triage running on the *user's* bill. Do not upgrade to Opus
# without a reason. cfg.model overrides this when non-empty.
MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000


def call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise MailError(
            "ANTHROPIC_API_KEY is not set. Add it in your fork: Settings -> Secrets and variables "
            "-> Actions -> New repository secret, named exactly ANTHROPIC_API_KEY. "
            "Get a key at https://console.anthropic.com/settings/keys"
        )
    import anthropic

    tool: ToolParam = {
        "name": "emit",
        "description": "Return the structured result.",
        "input_schema": schema,
    }
    # Forced, not suggested: the whole reply shape depends on this tool being called.
    tool_choice: ToolChoiceToolParam = {"type": "tool", "name": "emit"}
    messages: list[MessageParam] = [{"role": "user", "content": user}]
    try:
        resp = anthropic.Anthropic().messages.create(
            model=cfg.model or MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
            tools=[tool],
            tool_choice=tool_choice,
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

    usage = getattr(resp, "usage", None)
    log_usage(getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None))

    # These are API-shape concerns, not triage-pipeline ones -- they belong here,
    # not in the shared dispatcher, so every backend can fail on its own terms.
    if resp.stop_reason == "max_tokens":
        raise MailError(
            "the model's reply was cut off before it finished (stop_reason=max_tokens). Lower "
            "'reading_count' in config.yaml, or shorten the 'interests' text."
        )
    block = next((b for b in resp.content if b.type == "tool_use"), None)
    if block is None:
        raise MailError(
            "the model returned no triage at all, which usually means it declined the request. Check "
            "the 'interests' and 'avoid' text in config.yaml for anything it might refuse to act on."
        )
    result: dict[str, Any] = block.input
    return result
