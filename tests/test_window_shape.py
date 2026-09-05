"""What kind of mail a window held — the answer to "why was my digest empty?".

Two real slots delivered nothing from 14 and 23 candidates, and nothing in
the log could distinguish a working run over a promotional window from a
broken one over a real inbox. These pin the split, and pin that it stays
counts-only: the log this prints into is public on a fork.
"""

from __future__ import annotations

from mailtriage.imap_pull import window_shape
from mailtriage.models import Email


def _em(sender: str, unsubscribe: str = "") -> Email:
    em: Email = {
        "account": "reader@example.com",
        "from": sender,
        "subject": "s",
        "snippet": "x",
        "body": "x",
        "date": "2026-09-05T09:00:00+00:00",
        "unread": True,
        "link": "",
        "message_id": "<a@b>",
        "reply_to": sender,
        "uid": "1",
    }
    if unsubscribe:
        em["unsubscribe"] = unsubscribe
    return em


def test_splits_a_window_three_ways_and_totals():
    items = [
        _em("The Browser <hello@thebrowser.com>", "https://x.example/unsub"),
        _em("Substack <no-reply@substack.com>", "https://y.example/unsub"),
        _em("Uber Receipts <noreply@uber.com>"),
        _em("GitHub <notifications@github.com>"),
        _em("Priya Raghavan <priya@northwind.example>"),
    ]
    shape = window_shape(items)
    assert (shape.bulk, shape.automated, shape.people) == (2, 2, 1)
    assert shape.bulk + shape.automated + shape.people == len(items)


def test_unsubscribe_beats_a_noreply_sender():
    # A newsletter from no-reply@ is bulk, not automated: the sender declared
    # itself a mailing, which is the stronger signal.
    assert window_shape([_em("no-reply@sub.example", "https://a.example/u")]) == (1, 0, 0)


def test_an_empty_window_is_all_zeroes():
    assert window_shape([]) == (0, 0, 0)


def test_a_person_at_a_company_domain_counts_as_a_person():
    people = [
        _em("Anika Fowler <a.fowler@meridianhealth.example>"),
        _em("billing@figma.com"),  # no unsubscribe, not noreply-ish
        _em("frontdesk@bridgedental.example"),
    ]
    assert window_shape(people).people == 3


def test_noreply_variants_all_count_as_automated():
    for addr in (
        "no-reply@x.example",
        "noreply@x.example",
        "donotreply@x.example",
        "notifications@x.example",
        "notification@x.example",
        "mailer-daemon@x.example",
        "postmaster@x.example",
    ):
        assert window_shape([_em(addr)]).automated == 1, addr
