---
name: add-delivery
description: Use when adding a new delivery backend or channel to mailtriage (Telegram, Slack, webhook, SMS, another mail provider) or when asked "can it send to X".
---

# Adding a delivery backend

## Overview

Delivery is a dict dispatch keyed off `cfg.delivery`. A new backend touches
exactly seven places — miss one and forks break silently. `delivery/gmail.py`
is the reference: it was added through this exact checklist.

## The checklist

1. **`src/mailtriage/config.py`** — extend the Literal:
   `Delivery = Literal["email", "gmail", "<name>"]`. Nothing else parses the
   value; the Literal IS the validation.
2. **`src/mailtriage/delivery/<name>.py`** — module named to never shadow
   stdlib (`mail.py` not `email.py`). One function:
   `send(cfg: Config, triaged: list[Triaged]) -> None`.
   - Raise `MailError` with fix-it messages (what to change, where) — never
     exit, never print-and-continue.
   - HTML output? Reuse `email_html` from `delivery/mail.py`. Never duplicate
     the template.
   - Plain-text channel? Escape/limit per that channel's rules IN the module
     (e.g. Telegram: HTML parse mode, `html.escape` BEFORE wrapping in tags,
     chunk under the 4096 hard cap).
   - Stdlib transport first (`urllib` via `delivery/http.py`'s `post_json`,
     `smtplib`, …). A new pip dependency needs a reason written in the PR.
   - Secrets from env (`os.environ.get`), missing → `MailError` naming the
     exact repo-secret to add.
3. **`src/mailtriage/delivery/__init__.py`** — one `BACKENDS` entry.
4. **`tests/test_<name>.py`** — mock the transport, never the network.
   Copy the pattern from `tests/test_gmail.py` (fake SMTP object) or
   `tests/test_mail.py` (monkeypatched `post_json`). Cover: payload shape,
   escaping of a hostile subject, missing-secret → `MailError`, transport
   error → `MailError`.
5. **Docs** — `config.yaml` comment block (when to pick this mode, which
   secrets it needs) + README "Delivery options".
6. **Wizard** — only if the mode adds user-facing secrets/fields to
   `docs/index.html`. **REQUIRED SUB-SKILL: update-wizard** before touching it.
7. **THE GATE**, then ship via **create-pr**.

## Traps (each bit us once)

- `tests/test_config.py` must not pin the shipped `delivery:` value — it's
  user-editable; assert membership in the valid set instead.
- Secret names are a three-way contract (wizard writes, workflow exports,
  engine reads). A new secret name must appear in all three, and
  `tests/test_contracts.py` should learn about it.
- The workflow exports ALL secrets generically (`toJSON(secrets)`) — no
  digest.yml edit needed for a new secret, only for new install steps.
