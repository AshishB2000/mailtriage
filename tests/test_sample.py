"""docs/sample-digest.html is the wizard's preview of a digest. It must be the
real template's output, never a hand-edited copy -- regenerate it with
`python scripts/render_sample.py` whenever mail.email_html changes."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _render():
    spec = importlib.util.spec_from_file_location("render_sample", ROOT / "scripts" / "render_sample.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.render()


def test_committed_sample_matches_current_template():
    committed = (ROOT / "docs" / "sample-digest.html").read_text(encoding="utf-8")
    assert committed == _render(), (
        "docs/sample-digest.html is stale -- run `python scripts/render_sample.py` and commit the result."
    )


def test_sample_covers_every_section():
    html = _render()
    for text in ("Needs action", "Still waiting on you", "Worth reading", "Draft reply", "3d"):
        assert text in html
