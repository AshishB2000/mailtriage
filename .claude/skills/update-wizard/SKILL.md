---
name: update-wizard
description: Use before editing docs/setup.html (the setup wizard) — adding fields, changing secrets handling, touching the launch flow, or restyling the page.
---

# Updating the setup wizard

## Overview

`docs/setup.html` is one self-contained page that writes secrets and config
into a stranger's fork. Its bugs don't error — they break forks *silently*.
Know the invariants before editing; verify all of them after.

## Invariants — violating any one breaks forks

| Invariant | Why |
|---|---|
| `mailPwSlug` (JS) === `pw_env_var` (Python, imap_pull.py) character-for-character: `"MAIL_PW_" +` first 16 hex chars, upper-cased, of BLAKE2b-128 over the trimmed lower-cased address (`sodium.crypto_generichash(16, …)` ≡ `hashlib.blake2b(digest_size=16)`); the `// vector:` comment beside it must match `tests/test_contracts.py` | Wizard writes the secret, engine reads it — a divergent name means that account is silently skipped. The name is a hash so it never prints the address in the public Actions log |
| `buildYaml()` emits EXACTLY the `Config` dataclass field names (config.py) | Unknown keys only warn; a typo'd key is settings the engine ignores, no error |
| Secrets/token live ONLY in memory (`S.token`, `S.secrets`); the localStorage `KEEP` list never contains token, keys, or passwords | The page's trust story: plaintext never persists, never leaves the tab unencrypted |
| Every secret is sealed with libsodium `crypto_box_seal` against the repo public key before PUT | GitHub only ever receives ciphertext |
| Works from `file://`: no CDN, no external asset, `sodium.js` vendored and unmodified | Users may run it locally; a network asset also breaks the "everything in this tab" claim |
| `WORKFLOW = "digest.yml"` literal; every ref/branch uses `S.repo.default_branch`, never hardcoded `main` | Dispatch is by filename; forks may use any default branch |
| Exactly ONE AI secret written, per the user's provider picker (`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CODEX_AUTH_JSON`, `OPENAI_API_KEY`, or `GEMINI_API_KEY`) | Writing more than one lets a stale credential shadow the one the user chose |
| `buildYaml()` writes `provider:` to the picker's explicit choice, never `"auto"` | The user made a choice in the UI; "auto" would silently defer to `triage.PROVIDERS`' auto-detect order instead |
| `sk-ant-…`, `sk-proj-…` strings appear only as input `placeholder` attributes | Never a real value in the page |
| Async prefill must not clobber user input (`S.step !== 1` guard) | Race fixed once already — don't reintroduce || `MODES` === digest.yml's `workflow_dispatch` `inputs.mode.options`, same order; `SYNC_WORKFLOW = "upstream-sync.yml"` literal | The dashboard's Run now / doctor / weekly buttons send `inputs: {mode}`; a value the workflow doesn't list is a 422. Pinned by `tests/test_contracts.py` |
| The dashboard only ever *reads* secrets by name (`GET .../actions/secrets`) and never renders, stores, or compares a value | Its trust story is the same as the launch flow's: this tab never holds a plaintext it didn't just get from the user |
| The sample preview is `data-src="sample-digest.html"` — a relative sibling, loaded only when the `<details>` opens | Must work from `file://` and Pages; `tests/test_sample.py` regenerates it from the real template, never hand-edit it |


## After ANY edit, run all of this

```bash
.venv/bin/python -c "from html.parser import HTMLParser; HTMLParser().feed(open('docs/setup.html').read()); print('html ok')"
.venv/bin/python -m pytest tests/test_contracts.py -q   # machine-checks the mirrors
grep -c 'sk-ant' docs/setup.html                        # only placeholder lines
grep -n 'KEEP' docs/setup.html                          # eyeball: no secret ids in the list
grep -n 'src="http' docs/setup.html                     # must be empty (no CDN)
```

Then open the page from `file://` and click through step 1 rendering.
If you touched `delivery/mail.py`'s template, also run
`.venv/bin/python scripts/render_sample.py` and commit `docs/sample-digest.html`.

## Common mistakes

- Adding a config field in the wizard but not `config.py` (or vice versa) —
  the contract test catches it only if both sides changed names, so update
  `Config` first, wizard second.
- Testing only via GitHub Pages — `file://` is the contract; Pages hides
  missing-asset bugs.
- "Improving" `sodium.js` or swapping it for a CDN build.
