"""Claude via the `claude` CLI, for CLAUDE_CODE_OAUTH_TOKEN (subscription) auth.

The SDK is API-key only and cannot use a subscription token, so this is the
only way to triage against CLAUDE_CODE_OAUTH_TOKEN. `--json-schema` is the
CLI's only output-shaping knob — there's no forced-tool equivalent — but
`pick()` already treats the reply as hostile input, so that's fine.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

from mailtriage.config import Config
from mailtriage.errors import MailError

# The CLI's own default model is used unless cfg.model is set -- see call().
MODEL = ""


def call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    # `--system-prompt` replaces Claude Code's own agent persona with ours, so
    # the model is a triager, not a coding agent handed a list of emails.
    # `--model` is passed ONLY when the user set one: the subscription is
    # flat-rate, so the CLI's default model costs nothing extra, and the
    # working v1 invocation never pinned one. Pinning claude-sonnet-5 here
    # coincided with runs that returned zero items from 37 candidates on CLI
    # 2.1.259 (live, 2026-09-02) -- see the diagnostics line below.
    argv = [
        "claude",
        "-p",
        user,
        "--system-prompt",
        system,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        "--no-session-persistence",
    ]
    if cfg.model:
        argv += ["--model", cfg.model]
    try:
        proc = subprocess.run(
            argv,
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

    # Envelope metadata only -- which model actually answered, how it stopped,
    # whether a structured object came back. Never the result text: Actions
    # logs on a public fork are public. This is what turns "0 items" from a
    # mystery into a diagnosis.
    meta = {
        "models": sorted((parsed.get("modelUsage") or {}).keys()),
        "turns": parsed.get("num_turns"),
        "stop": parsed.get("stop_reason"),
        "terminal": parsed.get("terminal_reason"),
        "structured": isinstance(parsed.get("structured_output"), dict),
        "result_type": type(parsed.get("result")).__name__,
    }
    print(f"mailtriage: claude CLI envelope {meta}", file=sys.stderr)

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
