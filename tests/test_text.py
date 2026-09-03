"""text.py is the one digest renderer every non-HTML delivery shares."""

from __future__ import annotations

from mailtriage.delivery.text import DRAFT_PREVIEW, chunk, digest_text, html_to_text, render
from tests.helpers import item


def test_digest_text_sections_in_order_with_link_under_each_item():
    out = digest_text(
        [item("worth_reading", "read me"), item("carried", "still open"), item("needs_action", "do this")]
    )
    assert out.index("Needs action") < out.index("Still waiting on you") < out.index("Worth reading")
    assert "do this — Alice <alice@example.com> — worth a look\nhttps://mail.example.com/msg/1?a=1&b=2" in out


def test_digest_text_omits_empty_sections():
    out = digest_text([item("worth_reading")])
    assert "Needs action" not in out
    assert "Worth reading" in out


def test_digest_text_trims_the_draft_preview():
    long_draft = "word " * 100
    out = digest_text([item("needs_action", draft=long_draft)])
    line = next(line for line in out.splitlines() if line.startswith("Draft: "))
    assert len(line) - len("Draft: ") == DRAFT_PREVIEW
    assert line.endswith("…")


def test_digest_text_flattens_draft_newlines():
    out = digest_text([item("needs_action", draft="Hi,\n\nSounds good.")])
    assert "Draft: Hi, Sounds good." in out


def test_render_escapes_before_wrapping():
    out = render(
        [item("needs_action", "a <b> & c")],
        bold=lambda s: f"<b>{s}</b>",
        link=lambda text, url: f'<a href="{url}">{text}</a>',
        esc=lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
    )
    assert '<a href="https://mail.example.com/msg/1?a=1&b=2">a &lt;b&gt; &amp; c</a>' in out
    assert "<b>Needs action</b>" in out
    assert "\nhttps://" not in out  # inline links: no URL line


def test_chunk_fits_limit_and_drops_nothing():
    text = "\n\n".join(f"para{i} " + "x" * 500 for i in range(30))
    parts = chunk(text, 3900)
    assert len(parts) > 1
    assert all(len(p) <= 3900 for p in parts)
    joined = "".join(parts)
    assert all(f"para{i} " in joined for i in range(30))


def test_chunk_hard_splits_an_oversized_paragraph():
    assert chunk("y" * 250, 100) == ["y" * 100, "y" * 100, "y" * 50]


def test_html_to_text_keeps_text_and_links_drops_style():
    html = (
        "<html><head><style>body{color:red}</style></head><body>"
        '<div>Your week</div><table><tr><td><a href="https://x/1">Open one</a>'
        "<div>Bob &amp; Co &nbsp;·&nbsp; 3d</div></td></tr></table></body></html>"
    )
    out = html_to_text(html)
    assert "color:red" not in out
    assert out.splitlines()[0] == "Your week"
    assert "Open one\nhttps://x/1" in out
    assert "Bob & Co · 3d" in out
    assert "\n\n\n" not in out
