"""The one usage line every backend prints after a model call. Counts only --
Actions logs on a public fork are public. Lives in its own module because
triage/__init__ imports every backend at module scope, so a backend can't
import from it without a cycle."""

from __future__ import annotations

import sys
from typing import Any


def log_usage(input_tokens: Any, output_tokens: Any, cost_usd: Any = None) -> None:
    """Print `mailtriage: usage input=N output=N [cost=$X.XXXX]` to stderr.
    Prints nothing when the backend didn't expose integer token counts."""
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (input_tokens, output_tokens)):
        return
    line = f"mailtriage: usage input={input_tokens} output={output_tokens}"
    if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool):
        line += f" cost=${cost_usd:.4f}"
    print(line, file=sys.stderr)
