"""ChatGPT via the `codex` CLI, for CODEX_AUTH_JSON (subscription) auth.

The workflow provisions ~/.codex/auth.json from the CODEX_AUTH_JSON secret,
but CI is stateless -- this module writes that file itself from the env var
when it's set and the file is missing, so the same secret works locally too,
not just in Actions.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from mailtriage.config import Config
from mailtriage.errors import MailError


def _auth_path() -> Path:
    return Path.home() / ".codex" / "auth.json"


def _ensure_auth() -> None:
    path = _auth_path()
    if path.exists():
        return
    auth_json = os.environ.get("CODEX_AUTH_JSON")
    if not auth_json:
        raise MailError(
            "No Codex auth configured -- neither CODEX_AUTH_JSON is set nor ~/.codex/auth.json exists. Run "
            "`codex login` locally, then paste the full contents of ~/.codex/auth.json as the CODEX_AUTH_JSON "
            "repo secret."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(auth_json, encoding="utf-8")
    path.chmod(0o600)


def call(cfg: Config, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
    _ensure_auth()
    prompt = system + "\n\n" + user
    with tempfile.TemporaryDirectory() as tmp:
        schema_path = os.path.join(tmp, "schema.json")
        out_path = os.path.join(tmp, "out.txt")
        with open(schema_path, "w", encoding="utf-8") as f:
            json.dump(schema, f)

        try:
            proc = subprocess.run(
                [
                    "codex",
                    "exec",
                    prompt,
                    "--output-schema",
                    schema_path,
                    "--output-last-message",
                    out_path,
                    "--skip-git-repo-check",
                ],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except FileNotFoundError as e:
            raise MailError(
                "the `codex` CLI is not installed or not on PATH. Install it with `npm install -g "
                "@openai/codex`, or set OPENAI_API_KEY instead."
            ) from e

        detail = (proc.stderr or proc.stdout or "").strip()
        if proc.returncode != 0:
            raise MailError(_failure_message(proc.returncode, detail))

        try:
            with open(out_path, encoding="utf-8") as f:
                text = f.read()
            result: dict[str, Any] = json.loads(text)
        except (OSError, json.JSONDecodeError) as e:
            raise MailError(_failure_message(proc.returncode, detail, unparseable=True)) from e
    return result


def _failure_message(returncode: int, detail: str, unparseable: bool = False) -> str:
    # Codex rotates its tokens during use, and stateless CI can't persist that
    # rotation -- an auth failure here almost always means CODEX_AUTH_JSON is
    # now stale, not that anything else is wrong.
    if "401" in detail or "auth" in detail.lower():
        return (
            f"codex CLI {'produced unparseable output' if unparseable else f'exited with status {returncode}'}: "
            f"{detail[:500]} -- the tokens in CODEX_AUTH_JSON have likely expired or rotated. Codex rotates "
            "tokens during use and stateless CI can't persist that. Re-run `codex login` locally and re-paste "
            "the new ~/.codex/auth.json into the CODEX_AUTH_JSON secret, or switch to OPENAI_API_KEY."
        )
    if unparseable:
        return f"codex CLI produced unparseable output: {detail[:500]}"
    return f"codex CLI exited with status {returncode}: {detail[:500]}"
