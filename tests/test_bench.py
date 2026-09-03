"""The bench fixture and its scoring — the harness the prompt is tuned with.

No model call here: `--bench` makes the real one. These pin the things that
would silently make the benchmark meaningless — an expectation set that
drifted, a scorer that counts a miss as a pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mailtriage.bench import NEEDS_ACTION, NOISE, WORTH_READING, bench_inbox, score


def test_inbox_is_a_realistic_mix_not_a_pile_of_action_items():
    emails, expected = bench_inbox()
    assert len(emails) == len(expected)
    actions = sum(1 for e in expected if e == NEEDS_ACTION)
    noise = sum(1 for e in expected if e == NOISE)
    # A benchmark where most messages need action would pass by returning
    # everything, which is the opposite failure this product has.
    assert actions >= 5, "too few action items to measure recall"
    assert noise > actions, "a real window is mostly noise; this one isn't"


def test_every_email_is_valid_triage_input():
    emails, _ = bench_inbox()
    for em in emails:
        for key in ("account", "from", "subject", "snippet", "date", "link", "message_id"):
            assert key in em
        datetime.fromisoformat(em["date"])  # the +00:00 form triage parses
        assert em["uid"] == "", "synthetic mail must never carry a real UID"


def test_dates_are_inside_the_window_and_ordered_from_the_given_now():
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    emails, _ = bench_inbox(now)
    for em in emails:
        sent = datetime.fromisoformat(em["date"])
        assert now - timedelta(hours=15) <= sent <= now


def test_score_counts_a_missed_action_item():
    _emails, expected = bench_inbox()
    first_action = expected.index(NEEDS_ACTION)
    perfect = {i: b for i, b in enumerate(expected) if b != NOISE}

    assert score(expected, perfect)["missed"] == 0
    assert score(expected, {})["missed"] == expected.count(NEEDS_ACTION)

    one_short = {i: b for i, b in perfect.items() if i != first_action}
    assert score(expected, one_short)["missed"] == 1


def test_score_counts_noise_wrongly_called_an_action_item():
    _emails, expected = bench_inbox()
    noise_idx = expected.index(NOISE)
    got = {noise_idx: NEEDS_ACTION}
    result = score(expected, got)
    assert result["false_action"] == 1
    assert result["noise_returned"] == 1


def test_score_does_not_credit_a_demoted_action_item():
    _emails, expected = bench_inbox()
    idx = expected.index(NEEDS_ACTION)
    # Returned, but in the wrong bucket: the reader still never sees a to-do.
    assert score(expected, {idx: WORTH_READING})["missed"] == expected.count(NEEDS_ACTION)
