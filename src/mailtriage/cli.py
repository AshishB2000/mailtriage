"""Command line entry point. The only place that prints to the user or exits."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from email.utils import parseaddr
from typing import Any

from mailtriage import __version__
from mailtriage.calendar import today_events
from mailtriage.commands import apply_label_commands, count_done, derive_sender_rules, handle_replies, with_sender_rules
from mailtriage.config import Config, load_config
from mailtriage.errors import MailError
from mailtriage.imap_pull import (
    already_delivered,
    check_login,
    count_drafts,
    enrich,
    label_actions,
    label_noise,
    pull,
    pull_open_actions,
    pull_voice_examples,
    pull_week,
    push_drafts,
)
from mailtriage.models import Email, Event, Triaged, WeekResult
from mailtriage.schedule import current_slot, due, local_zone
from mailtriage.weekly import week_totals

UNSUBSCRIBE_CAP = 20  # senders in the digest's noise footer


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="mailtriage",
        description="Private AI triage of your Gmail inboxes on your schedule, delivered as an HTML email.",
    )
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="triage as normal (the API call still happens) and print the digest instead of sending it",
    )
    ap.add_argument("--self-check", action="store_true", help="run the built-in assertions and exit")
    ap.add_argument(
        "--weekly",
        action="store_true",
        help="send the weekly review (what got handled, what's still open) instead of a normal digest",
    )
    ap.add_argument(
        "--due",
        action="store_true",
        help=(
            "evaluate the schedule only, no network: exit 0 and print the due mode "
            "('digest' or 'weekly') to stdout if a run_at/weekly_review slot is due "
            "this hour, exit 3 if not (never 1, so a real failure is never confused "
            "with 'not due')"
        ),
    )
    ap.add_argument(
        "--doctor",
        action="store_true",
        help=(
            "diagnose the setup: config, each account's IMAP login, the AI provider on a fixed "
            "three-email fixture, and delivery (sends one real test message). One PASS/FAIL line per "
            "check on stderr; exit 1 if any failed"
        ),
    )
    # Not argparse's built-in action="version": that calls sys.exit() from inside
    # parse_args(), before main() gets a chance to return normally. Handled below
    # instead, so main() stays the only place that prints and exits.
    ap.add_argument("--version", action="store_true", help="print the version and exit")
    return ap


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_slot(cfg: Config, now_local: datetime) -> str:
    now_minutes = now_local.hour * 60 + now_local.minute
    ordered = sorted(cfg.run_at, key=lambda s: int(s[:2]) * 60 + int(s[3:5]))
    for slot in ordered:
        if int(slot[:2]) * 60 + int(slot[3:5]) > now_minutes:
            return slot
    return ordered[0]  # nothing left today -- wraps to tomorrow's first slot


def _due_any(cfg: Config, now: datetime, event: str) -> str | None:
    """schedule.due() over the base config -- or, with profiles, over each
    resolved profile: due if ANY of them is. "digest" beats "weekly" the same
    way due() itself ranks them; _run_profiles then runs each profile in its
    own due mode, so a profile whose weekly slot shares the hour with another
    profile's daily slot still gets its review."""
    if not cfg.profiles:
        return due(cfg, now, event)
    modes = {due(cfg.profile(name), now, event) for name in cfg.profiles}
    return "digest" if "digest" in modes else "weekly" if "weekly" in modes else None


def _handle_due(cfg: Config, now: datetime, event: str) -> int:
    """Print the due mode ('digest'/'weekly') to STDOUT and return the
    exit-code contract (0 due / 3 not due). All human-readable status lines
    go to stderr, so a caller can capture stdout as a clean mode string --
    see the "Is it time?" step in .github/workflows/digest.yml, which
    captures this to pick which of `mailtriage`/`mailtriage --weekly` to
    run next."""
    result = _due_any(cfg, now, event)
    if result == "digest":
        print("digest")
        return 0
    if result == "weekly":
        print("mailtriage: weekly review slot — running.", file=sys.stderr)
        print("weekly")
        return 0
    now_local = now.astimezone(local_zone(cfg.timezone))
    print(
        f"mailtriage: not due at {now_local:%H:%M} ({cfg.timezone}); "
        f"next slot {_next_slot(cfg, now_local)} — skipping.",
        file=sys.stderr,
    )
    return 3


def _print_digest(cfg: Config, kept: list[Triaged], today: date, events: list[Event] | None = None) -> None:
    # Same section order, #N numbering and (localized) headings as the HTML
    # -- delivery.mail owns all three, so a --dry-run transcript reads like
    # the email would in the reader's own language.
    from mailtriage.delivery.mail import (
        MAX_EVENTS,
        calendar_link,
        digest_groups,
        event_time,
        invite_numbers,
        section_heading,
        waiting_days,
    )
    from mailtriage.delivery.strings import t

    events = events or []
    if events:
        print(t(cfg, "today"))
        invites = invite_numbers(events, kept, today)
        for i, ev in enumerate(events[:MAX_EVENTS]):
            line = f"  {event_time(cfg, ev)} · {ev['summary'] or '(untitled)'}"
            if ev["location"]:
                line += f" · {ev['location']}"
            if i in invites:
                line += " · " + t(cfg, "invite_in_inbox", n=invites[i])
            print(line)
    n = 1
    for kind, key, items in digest_groups(kept, today):
        print(section_heading(cfg, key))
        for it in items:
            line = f"  #{n} {it['subject']} · {it['sender']}"
            if kind == "carried":
                days = waiting_days(it["date"])
                line += " · " + t(cfg, "waiting", days=t(cfg, "days", n=days))
                if days >= cfg.nag_after_days:
                    line += " · " + t(cfg, "still_open").upper()
            if it["note"]:
                line += f" · {it['note']}"
            print(line)
            if it.get("due"):
                print(f"    {t(cfg, 'due', date=it['due'])} · {calendar_link(it)}")
            if it["draft"]:
                print(f"    {t(cfg, 'draft_reply')}: {it['draft']}")
                if it.get("draft_full"):
                    print(f"    ({t(cfg, 'draft_full')})")
            n += 1
    noise = [x for x in kept if x["bucket"] == "noise"]
    if noise:  # unnumbered: not addressable by a reply, just links
        print(t(cfg, "noise_this_week"))
        for it in noise:
            print(f"  {it['sender']} · {it['link']}")


def _print_weekly(
    cfg: Config,
    week: WeekResult,
    done_count: int = 0,
    narrative: dict[str, Any] | None = None,
    totals: dict[str, int] | None = None,
) -> None:
    from mailtriage.delivery.mail import _saved_text

    if narrative:
        print(narrative["summary"])
        for p in narrative["patterns"]:
            print(f"  - {p}")
        print()
    if totals and (totals["triaged"] or totals["drafts"]):
        print(_saved_text(cfg, totals))
    if done_count:
        print(f"{done_count} marked done via the mailtriage/done label")
    for account, buckets in week["accounts"].items():
        replied, archived, open_items = buckets["replied"], buckets["archived"], buckets["open"]
        print(f"{account} — {len(replied)} replied · {len(archived)} archived · {len(open_items)} open")
        for it in sorted(open_items, key=lambda i: i["date"]):  # oldest first
            print(f"  {it['subject']} · {it['sender']} · {it['age_days']}d")


def _week_counts(week: WeekResult) -> tuple[int, int]:
    handled = sum(len(b["replied"]) + len(b["archived"]) for b in week["accounts"].values())
    still_open = sum(len(b["open"]) for b in week["accounts"].values())
    return handled, still_open


def _slot_stamp(cfg: Config, now: datetime, dry_run: bool) -> str:
    """The slot this scheduled run is for, e.g. "Thu 03 Sep 08:00" -- it goes
    in the subject and is what the no-double-send guard searches for. "" for
    a dry run or a manual run (GITHUB_EVENT_NAME=workflow_dispatch or unset),
    which are never stamped and never guarded."""
    if dry_run:
        return ""
    slot = current_slot(cfg, now, os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"))
    return f"{slot:%a %d %b %H:%M}" if slot else ""


def _slot_already_delivered(cfg: Config, stamp: str, now: datetime) -> bool:
    """Two hourly cron firings can both land inside one slot's catch_up_minutes
    window. Gmail is the memory: if any account already holds this slot's
    stamped subject, this run has nothing to do. Runs before pull/triage so
    the duplicate costs no API call either."""
    if stamp and already_delivered(os.environ, cfg.subject_prefix, stamp, now):
        print("mailtriage: this slot's digest was already delivered — sending nothing.", file=sys.stderr)
        return True
    return False


def _week_narrative(cfg: Config, week: WeekResult, done_count: int, now: datetime) -> dict[str, Any] | None:
    """The model-written opening, or None. A provider error here is a
    warning, never a lost review -- the plain roll-up still goes out."""
    if not cfg.weekly_narrative:
        return None
    from mailtriage.triage import select_backend
    from mailtriage.weekly import narrate_week

    try:
        _name, call = select_backend(cfg, os.environ)
        return narrate_week(cfg, call, week, done_count, now.astimezone(local_zone(cfg.timezone)).date())
    except MailError as e:
        print(f"mailtriage: weekly narrative failed, sending the plain review: {e}", file=sys.stderr)
        return None


def run_weekly(cfg: Config, dry_run: bool = False, only: set[str] | None = None) -> None:
    # Imported here, not at module scope: mirrors run()'s lazy delivery
    # import (weekly_html lives in delivery.mail, alongside the Resend
    # HTTP client) so --self-check keeps working the same way.
    from mailtriage.delivery import send_html
    from mailtriage.delivery.mail import weekly_html

    now = _now()
    stamp = _slot_stamp(cfg, now, dry_run)
    if _slot_already_delivered(cfg, stamp, now):
        return
    week = pull_week(os.environ, now, cfg.label, only=only)

    for w in week["warnings"]:
        print(f"mailtriage: account failed, skipping: {w}", file=sys.stderr)

    # Items closed with the done label have lost cfg.label, so pull_week
    # can't see them -- counted separately, search only.
    done = count_done(os.environ, now)
    for w in done["warnings"]:
        print(f"mailtriage: done-count lookup failed, skipping: {w}", file=sys.stderr)
    done_count: int = done["done"]

    handled, still_open = _week_counts(week)
    if handled == 0 and still_open == 0 and done_count == 0:
        # Same "send nothing" philosophy as the normal digest: a roll-up with
        # nothing to report trains you to ignore it.
        print("mailtriage: nothing this week — sending nothing.", file=sys.stderr)
        return

    narrative = _week_narrative(cfg, week, done_count, now)
    # An estimate, and the log says so -- see weekly.MINUTES_PER_*.
    totals = week_totals(week, done_count, count_drafts(os.environ, now))
    print(
        f"mailtriage: this week {totals['triaged']} triaged, {totals['drafts']} drafted "
        f"(~{totals['minutes']} min, estimated).",
        file=sys.stderr,
    )

    if dry_run:
        _print_weekly(cfg, week, done_count, narrative, totals)
        return

    head = f"{cfg.subject_prefix} · {stamp}" if stamp else cfg.subject_prefix
    send_html(cfg, f"{head} · weekly review", weekly_html(cfg, week, done_count, narrative, totals))
    done_part = f", {done_count} done" if done_count else ""
    print(
        f"mailtriage: weekly review delivered ({handled} handled{done_part}, {still_open} open) via {cfg.delivery}.",
        file=sys.stderr,
    )


def _carried_triaged(em: Email) -> Triaged:
    # idx=-1: carried items have no source index in this run's `emails` list
    # (they were pulled from Gmail by label, not by triage()) -- they never
    # go through drafts or the needs_action rules, both of which key on idx.
    return {
        "bucket": "carried",
        "note": "",
        "account": em["account"],
        "sender": em["from"],
        "subject": em["subject"],
        "link": em["link"],
        "date": em["date"],
        "unread": em["unread"],
        "idx": -1,
        "draft": "",
    }


def _noise_triaged(em: Email) -> Triaged:
    # idx=-1 like carried items: nothing downstream keys a noise row back to
    # the pulled list. link is the unsubscribe target, sender the display name.
    name, addr = parseaddr(em["from"])
    return {
        "bucket": "noise",
        "note": "",
        "account": em["account"],
        "sender": name or addr or em["from"],
        "subject": em["subject"],
        "link": em.get("unsubscribe", ""),
        "date": em["date"],
        "unread": em["unread"],
        "idx": -1,
        "draft": "",
    }


def run(cfg: Config, dry_run: bool = False, only: set[str] | None = None) -> None:
    # Imported here, not at module scope: --self-check must work on a machine
    # where `anthropic` failed to install, and this is the only path that needs it.
    from mailtriage.delivery import send
    from mailtriage.drafts import generate_drafts
    from mailtriage.rules import apply_ignore, enforce, omitted
    from mailtriage.triage import select_backend, triage

    now = _now()
    stamp = _slot_stamp(cfg, now, dry_run)
    if _slot_already_delivered(cfg, stamp, now):
        return
    result = pull(os.environ, now, cfg.window_hours, only=only)

    # A dead account must not fail the run: a red X for one broken account
    # trains you to ignore red X's. Report and carry on with the rest.
    for w in result["warnings"]:
        print(f"mailtriage: account failed, skipping: {w}", file=sys.stderr)

    emails = result["messages"]
    # Counts only -- never subjects or senders. Actions logs on a public fork
    # are public; this line is what lets someone debug "kept none" without
    # leaking what was in the inbox.
    n_accounts = len({e["account"] for e in emails})
    print(
        f"mailtriage: {len(emails)} candidate(s) in the last {cfg.window_hours}h across {n_accounts} account(s).",
        file=sys.stderr,
    )

    # Gmail as the control plane: labels the reader applied and replies they
    # sent to the last digest, acted on before anything else so this run
    # already reflects them. Writes, so skipped on a dry run; the never/vip
    # sender derivation is read-only and runs either way.
    today = now.astimezone(local_zone(cfg.timezone)).date()
    if not dry_run:
        labels = apply_label_commands(os.environ, today, cfg.label)
        for w in labels["warnings"]:
            print(f"mailtriage: label commands failed, skipping: {w}", file=sys.stderr)
        c = labels["counts"]
        print(f"mailtriage: labels: {c['done']} done, {c['snoozed']} snoozed, {c['woken']} woken.", file=sys.stderr)

        replies = handle_replies(cfg, os.environ, now, today, lambda: select_backend(cfg, os.environ)[1])
        for w in replies["warnings"]:
            print(f"mailtriage: digest reply handling failed, skipping: {w}", file=sys.stderr)
        rc = replies["counts"]
        applied = ", ".join(f"{rc[a]} {a}" for a in ("done", "snooze", "draft", "never", "vip") if rc[a])
        skipped = f", {rc['skipped']} skipped" if rc["skipped"] else ""
        print(
            f"mailtriage: {replies['replies']} digest repl(ies) handled ({applied or 'no commands'}{skipped}).",
            file=sys.stderr,
        )

        before = len(emails)
        skip_uids, skip_ids = labels["skip"], replies["skip_message_ids"]
        emails = [
            e for e in emails if e["uid"] not in skip_uids.get(e["account"], set()) and e["message_id"] not in skip_ids
        ]
        if before > len(emails):
            print(
                f"mailtriage: done/snoozed labels and digest replies dropped {before - len(emails)}.", file=sys.stderr
            )

    senders = derive_sender_rules(os.environ)
    for w in senders["warnings"]:
        print(f"mailtriage: never/vip lookup failed, skipping: {w}", file=sys.stderr)
    if senders["never"] or senders["vip"]:
        print(
            f"mailtriage: {len(senders['never'])} never-sender(s), {len(senders['vip'])} vip-sender(s).",
            file=sys.stderr,
        )
    cfg = with_sender_rules(cfg, senders["never"], senders["vip"])

    before = len(emails)
    emails = apply_ignore(cfg, emails)
    if before > len(emails):
        print(f"mailtriage: rules.always_ignore dropped {before - len(emails)} message(s).", file=sys.stderr)

    if not emails:
        print("mailtriage: nothing recent — sending nothing.", file=sys.stderr)
        return

    if cfg.thread_context or cfg.sender_memory:
        # Read-only lookups that give the model more to go on. Counts only.
        ctx = enrich(os.environ, emails, now, thread_context=cfg.thread_context, sender_memory=cfg.sender_memory)
        for w in ctx["warnings"]:
            print(f"mailtriage: context lookup failed, skipping: {w}", file=sys.stderr)
        print(
            f"mailtriage: thread context on {ctx['threads']} message(s) ({ctx['fetches']} extra fetch(es)); "
            f"sender history for {ctx['senders']} sender(s).",
            file=sys.stderr,
        )

    kept = triage(cfg, emails, now)
    kept = enforce(cfg, emails, kept)  # rule-forced items must survive even if the model returned none
    if not kept:
        # Carried items alone must never trigger a digest: this check runs
        # before any carry-over items are merged in, so "no new items" stays
        # "no new items" even when yesterday's debts are still open.
        print("mailtriage: the model kept none of the candidates — sending nothing.", file=sys.stderr)
        return

    if cfg.carry_over:
        if not dry_run:
            # No mailbox writes on a dry run: label only when actually delivering.
            for w in label_actions(os.environ, kept, emails, cfg.label):
                print(f"mailtriage: label failed, skipping: {w}", file=sys.stderr)

        # Reading is fine on a dry run -- only writes are skipped above.
        carried = pull_open_actions(os.environ, now, cfg.window_hours, cfg.label, only=only)
        for w in carried["warnings"]:
            print(f"mailtriage: carried-mail lookup failed, skipping: {w}", file=sys.stderr)
        kept = kept + [_carried_triaged(em) for em in carried["messages"]]

    if cfg.draft_replies and any(t["bucket"] == "needs_action" for t in kept):
        # select_backend is pure env inspection -- picking again here (instead
        # of threading a pick through triage()) keeps triage()'s signature
        # unchanged and costs nothing extra.
        _name, call = select_backend(cfg, os.environ)
        voice: dict[int, list[str]] = {}
        if cfg.draft_style["learn_voice"]:
            # Reading is fine on a dry run. The examples go into the prompt
            # only -- counts here, never the text.
            voice, voice_warnings = pull_voice_examples(os.environ, kept, emails)
            for w in voice_warnings:
                print(f"mailtriage: voice lookup failed, skipping: {w}", file=sys.stderr)
            n_action = sum(1 for t in kept if t["bucket"] == "needs_action")
            print(f"mailtriage: voice examples for {len(voice)} of {n_action} item(s).", file=sys.stderr)
        generate_drafts(cfg, call, emails, kept, voice)  # MailError here is fatal -- auth is auth.
        if not dry_run:
            # No mailbox writes on a dry run: push only when actually delivering.
            for w in push_drafts(os.environ, kept, emails):
                print(f"mailtriage: draft push failed, skipping: {w}", file=sys.stderr)

    # Everything the run left out, minus rule-protected senders -- the only
    # candidates the two noise stages below may touch.
    noise_idx = omitted(cfg, emails, kept)

    if cfg.noise["label"] and not dry_run:
        # Opt-in, and a write: never on a dry run. Archiving only removes the
        # \Inbox label -- nothing is deleted.
        touched, noise_warnings = label_noise(os.environ, emails, noise_idx, archive=cfg.noise["archive"])
        for w in noise_warnings:
            print(f"mailtriage: noise label failed, skipping: {w}", file=sys.stderr)
        verb = "labeled and archived" if cfg.noise["archive"] else "labeled"
        print(f"mailtriage: {touched} noise message(s) {verb}.", file=sys.stderr)

    if cfg.show_unsubscribe:
        # One footer row per omitted sender that offered an unsubscribe link,
        # newest first, capped. Appended last: nothing above this line
        # (labels, drafts, the "kept none" check) ever sees these rows.
        seen_senders: set[str] = set()
        noise: list[Triaged] = []
        for i in noise_idx:
            em = emails[i]
            sender = parseaddr(em["from"])[1].lower()
            if not em.get("unsubscribe") or sender in seen_senders:
                continue
            seen_senders.add(sender)
            noise.append(_noise_triaged(em))
            if len(noise) == UNSUBSCRIBE_CAP:
                break
        print(f"mailtriage: {len(noise)} unsubscribe link(s) in the noise footer.", file=sys.stderr)
        kept = kept + noise

    # Today's calendar rides along with a digest; it never conjures one up on
    # its own (the "kept none -> send nothing" return above still stands).
    events = today_events(os.environ, cfg, now)

    if dry_run:
        _print_digest(cfg, kept, today, events)
        return

    send(cfg, kept, stamp, events)
    print(f"mailtriage: delivered {len(kept)} item(s) via {cfg.delivery}.", file=sys.stderr)


# --- --doctor ------------------------------------------------------------


def _doctor_fixture() -> list[Email]:
    """Three synthetic emails with one unmistakable needs_action among them.
    A provider that can't put the contract request in needs_action is not
    going to triage a real inbox usefully either."""
    people = (
        (
            "Priya Shah <priya@colleague.example.com>",
            "Signed contract",
            "Can you send the signed contract by Friday? Legal needs it before the kickoff.",
        ),
        (
            "The Weekly Byte <news@newsletter.example.com>",
            "This week in tech: 10 stories you missed",
            "Welcome to this week's roundup. Unsubscribe at any time.",
        ),
        (
            "Shop <receipts@shop.example.com>",
            "Your receipt for order #48213",
            "Thanks for your purchase. Total charged: $24.00. No action needed.",
        ),
    )
    return [
        {
            "account": "you@example.com",
            "from": sender,
            "subject": subject,
            "snippet": text,
            "body": text,
            "date": "2026-09-01T09:00:00+00:00",
            "unread": True,
            "link": "https://mail.google.com/",
            "message_id": "",
            "reply_to": "",
            "uid": "",
        }
        for sender, subject, text in people
    ]


def _check(name: str, ok: bool, detail: str) -> bool:
    print(f"doctor: {'PASS' if ok else 'FAIL'} {name} — {detail}", file=sys.stderr)
    return ok


def run_doctor(config_path: str) -> int:
    """One PASS/FAIL line per check on stderr, with the fix in the FAIL
    line. The delivery check sends one real message -- that's the point."""
    from mailtriage.delivery import send_html
    from mailtriage.triage import select_backend, triage

    try:
        cfg = load_config(config_path)
    except MailError as e:
        _check("config", False, str(e))
        return 1
    ok = _check("config", True, f"{config_path} loads")

    try:
        for addr, count, err, caps in check_login(os.environ):
            ok &= _check(
                f"account {addr}",
                not err,
                # The capability summary is what tells a non-Gmail forker which
                # features their server supports: mode plus booleans, never a
                # mailbox name (this line goes to a public Actions log).
                f"ok: {count} in INBOX · {caps}" if not err else f"{err} — check the MAIL_PW_* secret for this address",
            )
    except MailError as e:
        ok = _check("accounts", False, str(e))

    try:
        name, _call = select_backend(cfg, os.environ)
        kept = triage(cfg, _doctor_fixture(), _now())
        actioned = any(t["bucket"] == "needs_action" for t in kept)
        ok &= _check(
            f"provider {name}",
            actioned,
            "the fixture's contract request came back as needs_action"
            if actioned
            else f"{len(kept)} item(s) came back, none needs_action — check 'interests'/'avoid' in {config_path} "
            "don't exclude direct requests from colleagues",
        )
    except MailError as e:
        ok = _check("provider", False, str(e))

    try:
        send_html(cfg, f"{cfg.subject_prefix} · doctor", "<p>mailtriage doctor: delivery works.</p>")
        ok &= _check(f"delivery {cfg.delivery}", True, "test message sent — check the inbox it should land in")
    except MailError as e:
        ok = _check(f"delivery {cfg.delivery}", False, str(e))

    return 0 if ok else 1


def _run_profiles(cfg: Config, weekly: bool, dry_run: bool) -> int:
    """One run per profile, over just that profile's accounts. On a scheduled
    run the gate only said *some* profile is due, so each profile is
    re-checked here and runs in its own due mode (or not at all); a manual
    "Run workflow" click or a local run does all of them in the asked mode.
    One failing profile is reported and the rest still run -- exit 1 at the
    end if any failed, same warn-and-continue spirit as a dead account."""
    now = _now()
    scheduled = os.environ.get("GITHUB_EVENT_NAME") == "schedule"
    failed: list[str] = []
    for name, spec in cfg.profiles.items():
        pcfg = cfg.profile(name)
        mode = due(pcfg, now, "schedule") if scheduled else ("weekly" if weekly else "digest")
        if mode is None:
            print(f"mailtriage: profile {name}: not due this hour — skipping.", file=sys.stderr)
            continue
        accounts = set(spec["accounts"])
        print(
            f"mailtriage: profile {name}: {mode} over {len(accounts)} account(s) via {pcfg.delivery}.", file=sys.stderr
        )
        try:
            (run_weekly if mode == "weekly" else run)(pcfg, dry_run=dry_run, only=accounts)
        except MailError as e:
            print(f"mailtriage: profile {name}: {e}", file=sys.stderr)
            failed.append(name)
    if failed:
        print(f"mailtriage: {len(failed)} profile(s) failed: {', '.join(failed)}.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.version:
        print(f"mailtriage {__version__}")
        return 0

    try:
        if args.self_check:
            from mailtriage.selfcheck import self_check

            self_check()
            return 0
        if args.due:
            event = os.environ.get("GITHUB_EVENT_NAME", "schedule")
            return _handle_due(load_config(args.config), _now(), event)
        if args.doctor:
            return run_doctor(args.config)

        cfg = load_config(args.config)
        if cfg.profiles:
            return _run_profiles(cfg, weekly=args.weekly, dry_run=args.dry_run)
        if args.weekly:
            run_weekly(cfg, dry_run=args.dry_run)
        else:
            run(cfg, dry_run=args.dry_run)
    except MailError as e:
        print(f"mailtriage: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
