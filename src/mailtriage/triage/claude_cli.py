"""Claude via the `claude` CLI, for CLAUDE_CODE_OAUTH_TOKEN (subscription) auth.

The SDK is API-key only and cannot use a subscription token, so this is the
only way to triage against CLAUDE_CODE_OAUTH_TOKEN. `--json-schema` is the
CLI's only output-shaping knob — there's no forced-tool equivalent — but
`pick()` already treats the reply as hostile input, so that's fine.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from mailtriage.config import Config
from mailtriage.errors import MailError

# cfg.model overrides this when non-empty.
MODEL = "claude-sonnet-5"


def call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    prompt = system + "\n\n" + user
    try:
        proc = subprocess.run(
            [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(schema),
                "--model",
                cfg.model or MODEL,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except FileNotFoundError as e:
        raise MailError(
            "the `claude` CLI is not installed or not on PATH. Install it (see "
            "https://docs.claude.com/en/docs/claude-code) or set ANTHROPIC_API_KEY instead."
        ) from e

    # `claude -p --output-format json` prints a JSON envelope to STDOUT for both
    # success and failure (failures carry `is_error: true` and a human-readable
    # `result`), and typically leaves stderr empty. Parse stdout first so the
    # real reason — usually an auth failure — is surfaced instead of a blank.
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise MailError(
            f"`claude` CLI exited with status {proc.returncode} and unparseable output: {detail} — "
            "check that CLAUDE_CODE_OAUTH_TOKEN is a valid token (regenerate with `claude setup-token`)."
        ) from e

    if parsed.get("is_error") or proc.returncode != 0:
        reason = parsed.get("result") or proc.stderr.strip() or "unknown error"
        raise MailError(
            f"`claude` CLI could not triage: {reason} — if this is an authentication error, your "
            "CLAUDE_CODE_OAUTH_TOKEN is invalid or expired; regenerate it with `claude setup-token` and "
            "update the repo secret, or switch to ANTHROPIC_API_KEY instead."
        )

    structured = parsed.get("structured_output")
    if not isinstance(structured, dict):
        # Some CLI versions return the schema-constrained object as a JSON string
        # in `result` rather than a `structured_output` field.
        result = parsed.get("result")
        if isinstance(result, str):
            try:
                maybe = json.loads(result)
            except json.JSONDecodeError:
                maybe = None
            if isinstance(maybe, dict):
                structured = maybe
    if not isinstance(structured, dict):
        raise MailError("claude CLI returned no structured output — the run may have failed silently.")
    return structured
