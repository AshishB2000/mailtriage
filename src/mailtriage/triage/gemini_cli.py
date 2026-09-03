"""Google account subscription via the `gemini` CLI, for GEMINI_OAUTH_JSON auth.

60 RPM / 1,000 requests/day free on a personal Google account -- no API key,
no billing. The workflow provisions ~/.gemini/oauth_creds.json from the
GEMINI_OAUTH_JSON secret, but CI is stateless -- this module writes that file
itself from the env var when it's set and the file is missing, same pattern
as codex_cli._ensure_auth, so the same secret works locally too.

There is no JSON-schema constraint flag on the CLI, so the schema is appended
to the prompt as an explicit instruction and the reply is parsed defensively.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from mailtriage.config import Config
from mailtriage.errors import MailError
from mailtriage.triage.usage import log_usage

# cfg.model overrides this when non-empty.
MODEL = "gemini-2.5-flash"


def _gemini_home() -> Path:
    return Path(os.environ.get("GEMINI_CLI_HOME") or (Path.home() / ".gemini"))


def _ensure_auth() -> None:
    home = _gemini_home()
    creds_path = home / "oauth_creds.json"
    if creds_path.exists():
        return
    oauth_json = os.environ.get("GEMINI_OAUTH_JSON")
    if not oauth_json:
        raise MailError(
            "Run `gemini` locally once, sign in with your Google account, then paste the full contents of "
            "~/.gemini/oauth_creds.json as the GEMINI_OAUTH_JSON repo secret (Settings -> Secrets and "
            "variables -> Actions)."
        )
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    creds_path.write_text(oauth_json, encoding="utf-8")
    creds_path.chmod(0o600)

    settings_path = home / "settings.json"
    if not settings_path.exists():
        settings_path.write_text(
            json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}), encoding="utf-8"
        )


def _extract_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```").strip()
        if stripped.endswith("```"):
            stripped = stripped[: -len("```")].strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(stripped[start : end + 1])
    raise json.JSONDecodeError("no JSON object found", stripped, 0)


def call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    _ensure_auth()
    prompt = (
        system
        + "\n\n"
        + user
        + "\n\nRespond with ONLY a JSON object matching this schema, no prose, no markdown fences:\n"
        + json.dumps(schema)
    )
    model = cfg.model or MODEL
    env = dict(os.environ)
    env["GOOGLE_GENAI_USE_GCA"] = "true"
    env.pop("GEMINI_API_KEY", None)  # never let the CLI silently fall back to API-key mode

    try:
        proc = subprocess.run(
            ["gemini", "-p", prompt, "--output-format", "json", *(["-m", model] if model else [])],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
            check=False,
        )
    except FileNotFoundError as e:
        raise MailError(
            "the `gemini` CLI is not installed or not on PATH. Install it with `npm install -g "
            "@google/gemini-cli`, or set GEMINI_API_KEY to use the API instead."
        ) from e

    try:
        envelope: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        detail = (proc.stdout or proc.stderr or "").strip()
        raise MailError(f"gemini CLI produced unparseable output: {detail[:500]}") from e

    err = envelope.get("error")
    if proc.returncode != 0 or err:
        raise MailError(_failure_message(proc.returncode, err))

    # `stats.models.<model>.tokens.{prompt,candidates}` -- summed across
    # models; absent on older CLIs, in which case nothing is printed.
    models = ((envelope.get("stats") or {}).get("models") or {}).values()
    tokens = [m.get("tokens") or {} for m in models if isinstance(m, dict)]
    if tokens and all(isinstance(t.get("prompt"), int) and isinstance(t.get("candidates"), int) for t in tokens):
        log_usage(sum(t["prompt"] for t in tokens), sum(t["candidates"] for t in tokens))

    text = envelope.get("response")
    if not isinstance(text, str):
        raise MailError("gemini CLI returned no JSON object -- its response envelope had no 'response' text.")

    try:
        result = _extract_json(text)
    except json.JSONDecodeError as e:
        raise MailError(f"gemini CLI returned no JSON object -- could not parse its response: {text[:500]}") from e
    if not isinstance(result, dict):
        raise MailError(f"gemini CLI returned no JSON object -- got {type(result).__name__}: {text[:500]}")
    return result


def _failure_message(returncode: int, err: dict[str, Any] | None) -> str:
    message = str(err.get("message", "")) if err else ""
    err_type = str(err.get("type", "")) if err else ""
    text = f"{err_type} {message}".lower()

    if returncode == 41 or "auth" in text:
        extra = ""
        if "GOOGLE_CLOUD_PROJECT" in message:
            extra = " Workspace/school accounts also need a GOOGLE_CLOUD_PROJECT environment variable set."
        return (
            f"gemini CLI exited with status {returncode} (authentication error): {message or err_type} -- the "
            "Google sign-in in GEMINI_OAUTH_JSON is invalid or was revoked. Google refresh tokens die if unused "
            "for 6 months or if access was revoked. Re-run `gemini` locally, sign in again, and re-paste the new "
            f"~/.gemini/oauth_creds.json into the GEMINI_OAUTH_JSON secret.{extra}"
        )
    if err:
        return f"gemini CLI exited with status {returncode}: {message or err_type}"
    return f"gemini CLI exited with status {returncode}"
