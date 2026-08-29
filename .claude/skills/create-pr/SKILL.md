---
name: create-pr
description: Use when asked to open a PR, push a branch, merge work, or ship a change in the mailtriage repo — including "small" or "urgent" fixes.
---

# Creating a PR in mailtriage

## Overview

Every change reaches `main` through a branch + PR with the gate green. During
this project's live debugging, fixes went straight to main — acceptable only in
an active firefight with the user watching, never the default.

## The workflow

1. **Branch off fresh main** — never commit on main:
   ```bash
   git checkout main && git pull --ff-only origin main
   git checkout -b <type>/<short-name>     # feat/ fix/ docs/ ci/ refactor/
   ```
2. **Do the work.** Commit messages: `type: imperative summary`, ending with
   the `Co-Authored-By: Claude` trailer.
3. **Run THE GATE — all four, locally, before push:**
   ```bash
   .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/python -m pytest -q
   ```
   Any red = fix before pushing. Changed the workflow YAML? Remember: YAML
   parsing does NOT validate Actions expressions (see CLAUDE.md landmines).
4. **Push and open the PR:**
   ```bash
   git push -u origin <branch>
   gh pr create --base main --title "..." --body "..."
   ```
   Body = What / Why / Verification (what you ran and its result), ending with
   the 🤖 Claude Code attribution line.
5. **Merge only when asked**: `gh pr merge <n> --squash --delete-branch`, then
   `git checkout main && git pull --ff-only origin main`.

## Direct-to-main is allowed ONLY when

All three hold: a live run is broken now, the user is actively watching, and
the change is the minimal fix. Even then: run the gate first, and say plainly
that you're pushing to main and why.

## Red flags — stop and branch

- "It's a one-line fix" / "just docs" / "I'll gate it after pushing"
- "CI will catch it" — CI failing on main is the failure
- Committing on main because the previous firefight left you there
