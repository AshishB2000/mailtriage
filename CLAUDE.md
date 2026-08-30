# mailtriage — working notes for Claude

Fork-and-run AI triage of Gmail inboxes. Users **fork** this repo and run it on
their own GitHub Actions with their own credentials. No server, no database, no
accounts. The maintainer never pays for anyone else's inference.

## Commands

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/python -m pytest -q
                                    # THE GATE — all four, before every push
.venv/bin/mailtriage --self-check   # assertions only, no network, no API, no creds
.venv/bin/mailtriage --dry-run      # real fetch + triage, prints instead of sends
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

gh workflow run digest.yml --ref main          # trigger a live run
gh run view <id> --log-failed                  # why it failed
gh release create vX.Y.Z --target main ...     # release = tag + notes; bump pyproject + __init__ first
```

## Layout

```
src/mailtriage/
  errors.py     MailError — the ONLY exception raised on purpose
  models.py     Email (triage input), Triaged (output), PullResult
  imap_pull.py  read-only IMAP fetch; pw_env_var is the secret-name transform;
                push_drafts APPENDs to \Drafts only
  config.py     config.yaml -> validated Config dataclass
  triage/       package, dict-dispatch pattern   <- the product
                  __init__.py: prompt + schema + pick() + PROVIDERS
                  claude_api.py claude_cli.py codex_cli.py openai_api.py gemini_api.py
  drafts.py     reply-drafting prompt + hostile-input-safe id mapping
  delivery/     dispatch, http.py, mail.py (Resend), gmail.py (own-Gmail SMTP)
  selfcheck.py  pre-flight assertions, run before any API spend
  cli.py        argparse; the only module that prints and exits
docs/index.html the zero-backend setup wizard (+ vendored sodium.js)
```

**Library code raises `MailError`, it never exits.** Only `cli.main()` catches,
prints to stderr, returns 1. Messages are written for someone staring at a red
workflow log — say what to change and where.

## The contracts — break these and forks silently break

**config.yaml** is written by the wizard, read by the engine, shipped as the
committed default. The field names on `config.Config` ARE the contract; unknown
keys warn, they never fail. `tests/test_config.py` loads the committed file.

**Secret names** — used by the wizard (writes), the workflow (exports all via
`toJSON(secrets)`), and the engine (reads env):

```
one of: CLAUDE_CODE_OAUTH_TOKEN  ANTHROPIC_API_KEY  CODEX_AUTH_JSON
        OPENAI_API_KEY           GEMINI_API_KEY        (exactly one AI secret)
RESEND_API_KEY      MAIL_ACCOUNTS      MAIL_PW_<SLUG> (one per address)
```

The wizard's provider picker writes an explicit `provider:` (never `"auto"`)
and PUTs exactly the one secret for that provider — the other four are never
written. `"auto"` (config.yaml default for hand-edited files) walks
`triage.PROVIDERS` in order and takes the first secret that's set.

`MAIL_PW_<SLUG>` = `"MAIL_PW_" + addr.upper()` with every non-alphanumeric →
`_`. This transform exists in TWO places that must match character-for-character:
`imap_pull.pw_env_var` (Python) and `mailPwSlug` (JS, docs/index.html).

**The workflow must stay at `.github/workflows/digest.yml`** — the wizard
dispatches it by that literal filename.

**The bucket contract**: model returns integer `id` + `bucket` + one `note`,
nothing else. `needs_action` is UNCAPPED (hiding an action item is the worst
failure). `worth_reading` caps at `reading_count`, enforced client-side in
`pick()`. Noise is never returned — omission is the label. `pick()` copies
sender/subject/link from the real Email, never from the model.

Model: `claude-sonnet-5` (API path). This is headline triage on the user's
bill; don't upgrade to Opus without a reason.

## Things that look wrong but are deliberate

- **No state, no seen-list.** `window_hours` (13) is the dedupe; it must stay ≥
  the largest cron gap (12h) or mail is skipped forever.
- **Empty digest sends nothing and exits 0.** "Nothing today" mails train users
  to unsubscribe.
- **A failed account warns and the run continues** — one bad login never blanks
  the digest. The broad `except Exception` per account is deliberate (imaplib
  raises 6+ unrelated types); `BLE001` is ignored repo-wide for this.
- **IMAP is read-only**: `select("INBOX", readonly=True)` + `BODY.PEEK[]`.
  Never let a change reintroduce plain `BODY[]` — it marks mail as read.
- **`triage/` never imports `anthropic` at module top.** `--self-check` must
  pass with the SDK uninstalled; the import lives inside `claude_api.call` only.
- **`delivery/mail.py` is named `mail`, never `email`** — shadows stdlib.
- **docs/sodium.js is vendored, not CDN** — the wizard must work from `file://`.
- **Provider auto-order is user-visible behavior, not an implementation
  detail.** `triage.PROVIDERS`' order — `claude-subscription` →
  `claude-api` → `chatgpt-subscription` → `openai-api` → `gemini-api` — is
  the precedence `"auto"` walks. Reordering it silently moves which secret
  (and which bill) an existing multi-secret fork lands on next run.
- **Gemini's `responseSchema` rejects `additionalProperties`.** `gemini_api.py`
  strips that keyword recursively before sending the schema. Don't "fix" the
  stripping away — Gemini 400s on the unmodified schema.
- **`CODEX_AUTH_JSON` tokens rotate**, and a stateless CI runner can't write
  the rotated value back to the secret. That's not a bug to fix — the 401
  error message already tells the user to re-run `codex login` and re-paste.
- **Drafting never sends and never touches an existing message.**
  `push_drafts` only `APPEND`s to the account's `\Drafts` mailbox; INBOX
  stays `select(..., readonly=True)` for the whole run; there is no SMTP call
  anywhere in `imap_pull.py`. Don't add one.

## Landmines (each one cost a real debugging session)

- **`secrets` context is illegal in a step-level `if:`.** GitHub rejects the
  whole workflow at dispatch time ("Unrecognized named-value: 'secrets'").
  Hoist to job-level `env:` and gate on `env.X`. Plain YAML parsing does NOT
  catch this.
- **`claude -p` reports errors on STDOUT, not stderr** — a JSON envelope with
  `is_error: true` and the reason in `result`, exit 1, stderr empty. Parse
  stdout first or every failure looks blank.
- **OAuth tokens picked up from a wrapped terminal line contain spaces.**
  A `sk-ant-oat01-…` token is ONE unbroken string; 401 "invalid" usually means
  a copy-paste space. Gmail app passwords are the opposite: displayed WITH
  spaces, stripped on use.
- **Resend cannot send from gmail.com** (403 "domain is not verified" — reads
  like a bad key, isn't). Options: verified domain, `onboarding@resend.dev`
  (spam-prone), or `delivery: gmail` which sends via the user's own Gmail SMTP
  reusing their existing MAIL_PW_* secret — no domain, lands in inbox.
- **Resend `to` must be a list**; a bare string 422s.
- **New GitHub accounts get an "Approve and run" gate** on Actions; scheduled
  runs sit waiting until the account's email is verified. Also: scheduled
  workflows auto-disable after 60 days without commits.
- **The wizard writes `provider:` explicitly, never `"auto"`.** The user made
  a choice in the picker; treat it as authoritative. It also PUTs exactly one
  of the five AI secrets — writing more than one lets a stale credential
  shadow the one the user meant to use.

## Style

Stdlib first — runtime deps are exactly `anthropic` + `PyYAML`, pinned. No
classes where functions work. Modules split by pipeline stage, flat tree.
`docs/index.html` is one self-contained file on purpose.

The triage prompt (`build_system`) is the product. Everything else is plumbing.
"Return fewer, never pad" is stated three ways on purpose — do not condense it.
