---
name: tune-triage
description: Use when the digest picks the wrong emails — misses action items, surfaces noise, pads worth-reading, buckets things wrongly — or the user wants to change what counts as important.
---

# Tuning what mailtriage considers important

## Overview

"What's important" lives in exactly three places, in escalating order of blast
radius. Fix at the lowest rung that solves it — most complaints are rung 1.

## The ladder

| Rung | Where | When | Blast radius |
|---|---|---|---|
| 1 | `config.yaml` → `interests:` / `avoid:` | A *category* is mis-handled ("bank alerts are noise to it, they're urgent to me") | This user only; no code, no gate |
| 2 | `triage.py` → `build_system()` | The *rules themselves* are wrong for everyone (bucket definitions, never-pad discipline) | Every fork; full PR + gate |
| 3 | `config.yaml` → `reading_count` / `window_hours` | Right items, wrong *volume* or *coverage window* | This user only |

**Rung 1 details:** write plain English, concrete over abstract. "Invoices and
anything with a deadline need action" beats "important financial matters".
`avoid` is for named noise categories. Commit `config.yaml` — it's meant to be
committed, holds no secrets.

**Rung 2 warnings:** the prompt IS the product. "Return fewer, never pad" is
stated three ways (permission / justification / consequence) — models treat a
count as a target and one polite "you may return fewer" gets ignored; never
condense it. `needs_action` stays uncapped: hiding an action item is the worst
failure this product can have. Noise is dropped by omission — never add a
returned noise bucket; a list nobody reads is padding with extra steps.

**Never a tuning surface:** `pick()` in triage.py. It's the trust boundary —
validates ids, enforces the cap, copies real fields over model output. Loosening
it to "fix" triage lets a hallucinating model corrupt the digest.

## Verify a change

```bash
.venv/bin/mailtriage --dry-run    # real fetch + triage, prints, sends nothing
```

Needs env: MAIL_ACCOUNTS + MAIL_PW_* + one AI token. Judge against the actual
inbox: did the misfiled item move? Did anything regress? Rung-2 changes also
need the full gate (see CLAUDE.md) and a PR.

## Common mistakes

- Editing the prompt for one person's preference → that's rung 1, config.
- Adding keyword if/else filters in Python → the model is the classifier;
  encode preferences in `interests`/`avoid` prose.
- Raising `reading_count` because "it might miss something" → capacity isn't
  the failure mode; padding is. Fix `interests` instead.
