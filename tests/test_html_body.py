"""HTML-only mail must still reach the model with a body.

A real 41-message window triaged to nothing while the same prompt scored
24/24 on a fixture, and the difference was this: the fixture's snippets were
populated and half the real mail was HTML-only, so it arrived as a subject
line with an empty body. The mail that needs action most -- invoices, e-sign
requests, bank alerts, appointment confirmations -- is exactly the mail that
gets sent HTML-only.
"""

from __future__ import annotations

from email import message_from_string, policy
from email.message import EmailMessage

from mailtriage.imap_pull import html_to_text, plain_text, snippet_of


def _msg(raw: str) -> EmailMessage:
    msg = message_from_string(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)
    return msg


HTML_ONLY = """From: billing@example.com
Subject: Your card will expire
Content-Type: text/html; charset="utf-8"

<html><head><style>.x{color:red}</style><title>ignored</title></head>
<body><p>We couldn't validate the card on file.</p>
<a href="https://example.com/pay">Update your payment method</a>
<script>track();</script></body></html>
"""

MULTIPART = """From: a@example.com
Subject: Both parts
Content-Type: multipart/alternative; boundary="b"

--b
Content-Type: text/plain; charset="utf-8"

the plain part
--b
Content-Type: text/html; charset="utf-8"

<p>the html part</p>
--b--
"""

EMPTY_PLAIN = """From: a@example.com
Subject: Blank plain part
Content-Type: multipart/alternative; boundary="b"

--b
Content-Type: text/plain; charset="utf-8"


--b
Content-Type: text/html; charset="utf-8"

<p>only the html says anything</p>
--b--
"""


def test_html_only_message_yields_a_body():
    text = plain_text(_msg(HTML_ONLY))
    assert "couldn't validate the card" in text
    assert "Update your payment method" in text


def test_html_extraction_drops_script_style_and_title():
    text = plain_text(_msg(HTML_ONLY))
    for junk in ("color:red", "track()", "ignored", "<p>", "href"):
        assert junk not in text


def test_text_plain_still_wins_when_present():
    assert plain_text(_msg(MULTIPART)).strip() == "the plain part"


def test_blank_text_plain_falls_through_to_html():
    # Senders that ship an empty text/plain part alongside a real HTML one are
    # common; treating the empty part as "the body" is the same bug.
    assert "only the html says anything" in plain_text(_msg(EMPTY_PLAIN))


def test_snippet_of_html_only_is_not_empty():
    assert snippet_of(_msg(HTML_ONLY)).startswith("We couldn't validate")


def test_html_to_text_decodes_entities_and_collapses_whitespace():
    assert html_to_text("<p>Pay   &amp; \n confirm</p>") == "Pay & confirm"


def test_html_to_text_survives_malformed_markup():
    assert "still here" in html_to_text("<p>still here<<<>&nbsp;<div")
