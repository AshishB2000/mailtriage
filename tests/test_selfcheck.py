"""self_check() is the fast, offline gate the digest workflow runs before any
API spend. This just proves it runs clean and prints its ok marker."""

from __future__ import annotations

from mailtriage.selfcheck import self_check


def test_self_check_passes(capsys):
    self_check()
    assert "self-check: ok" in capsys.readouterr().out
