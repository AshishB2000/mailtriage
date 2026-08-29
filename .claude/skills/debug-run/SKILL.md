---
name: debug-run
description: Use when a digest GitHub Actions run fails, shows action_required, hangs waiting for approval, sends nothing, or the user reports no email arrived.
---

# Debugging a failed digest run

## Get the evidence first

```bash
gh run list --workflow=digest.yml --limit 3
gh run view <id>                 # which step failed
gh run view <id> --log-failed    # the actual error
```

Never guess from the symptom alone — every failure below was misdiagnosed at
least once before the log settled it.

## Symptom → cause → fix

| Symptom | Cause | Fix |
|---|---|---|
| `action_required`, 0 jobs, "completed" in 1s | GitHub's new-account gate, not our code | User clicks "Approve and run"; permanent fix = verify email at github.com/settings/emails |
| Workflow rejected at dispatch: "Unrecognized named-value: 'secrets'" | `secrets` context in a step-level `if:` | Hoist to job-level `env:` (`HAS_X: ${{ secrets.X != '' }}`), gate on `env.HAS_X` |
| `` `claude` CLI could not triage: Failed to authenticate … 401 `` | OAuth token invalid — usually copy-paste spaces from a wrapped terminal line | Regenerate `claude setup-token`, paste as ONE unbroken `sk-ant-oat01-…` string into the secret |
| `claude` exits 1, error looks blank | CLI errors arrive as JSON on STDOUT (`is_error`/`result`), stderr empty | Engine now surfaces this; if blank again, read the run's raw stdout |
| Resend 403 "domain is not verified" | `email_from` not on a verified domain (gmail.com never can be). NOT a bad API key | Verify a domain, or switch `delivery: gmail` (sends via user's own Gmail, reuses MAIL_PW_*) |
| Resend 422 | `to` sent as bare string | Must be a list — engine does this; check any new payload code |
| Gmail SMTP auth error | App password wrong/revoked for `email_from` | Fresh one at myaccount.google.com/apppasswords; 16 chars, spaces stripped; secret name = `pw_env_var(email_from)` |
| One account skipped, run green | Per-account warning (by design) — usually a mistyped `MAIL_PW_<SLUG>` name | Recompute slug: upper-case address, non-alphanumerics → `_` |
| Green run, no email | Empty digest sends nothing (by design) — or first mail from a shared sender in spam | Check log for "delivered N item(s)"; if delivered, check spam |
| Scheduled runs stopped after weeks | 60-day auto-disable on inactive repos, or the approval gate | Re-enable in Actions tab / push any commit; verify account email |
| "No Claude auth configured" | Neither token secret set — or an invalid `CLAUDE_CODE_OAUTH_TOKEN` still present shadowing a good API key (token wins) | Set exactly one; delete the stale token secret |

## After fixing

Cheap local check first when creds are available — the binary is `mailtriage`,
nothing else:

```bash
.venv/bin/mailtriage --dry-run    # real fetch + triage, prints, sends nothing
```

Then re-trigger and watch to completion — `in_progress` is not success:

```bash
gh workflow run digest.yml --ref main
gh run view <new-id> --json status,conclusion    # poll until completed/success
```

Then confirm the log line `mailtriage: delivered N item(s) via <backend>.`
