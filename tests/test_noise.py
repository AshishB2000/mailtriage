"""label_noise is the opt-in noise stage: a Gmail label on what a run left
out, and -- only with archive=True -- the \\Inbox label removed so it leaves
the inbox while staying in All Mail. Never a delete, never a body fetch,
never a rule-protected sender (cli hands it rules.omitted). Fake IMAP4_SSL,
same pattern as tests/test_labels.py.
"""

from __future__ import annotations

import imaplib
from typing import Any

from mailtriage.config import Config
from mailtriage.imap_pull import label_noise
from mailtriage.rules import omitted
from tests.test_labels import PW_VAR, make_email, make_triaged


class _FakeIMAP:
    def __init__(self, host: str, port: int, *, login_error: Exception | None = None) -> None:
        self._login_error = login_error
        self.select_calls: list[tuple[Any, ...]] = []
        self.create_calls: list[str] = []
        self.store_calls: list[tuple[Any, ...]] = []

    def login(self, user: str, pw: str) -> tuple[str, list[bytes]]:
        if self._login_error:
            raise self._login_error
        return "OK", [b"ok"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        return "OK", [b"1"]

    def create(self, name: str) -> tuple[str, list[bytes]]:
        self.create_calls.append(name)
        return "NO", [b"[ALREADYEXISTS]"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        assert command == "STORE", f"label_noise must only STORE, got {command}"
        self.store_calls.append(args)
        return "OK", [b"1 (X-GM-LABELS (...))"]

    def expunge(self) -> None:  # pragma: no cover
        raise AssertionError("label_noise must never EXPUNGE")

    def fetch(self, *a: object, **k: object) -> None:  # pragma: no cover
        raise AssertionError("label_noise must never FETCH a body")

    def logout(self) -> tuple[str, list[bytes]]:
        return "OK", [b"bye"]


class _Factory:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.instances: list[_FakeIMAP] = []

    def __call__(self, host: str, port: int) -> _FakeIMAP:
        inst = _FakeIMAP(host, port, **self.kwargs)  # type: ignore[arg-type]
        self.instances.append(inst)
        return inst


def _patch(monkeypatch: Any, **kwargs: object) -> _Factory:
    factory = _Factory(**kwargs)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", factory)
    return factory


def test_label_only_adds_the_noise_label_and_never_touches_inbox_label(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch)
    emails = [make_email(0, uid="501"), make_email(1, uid="502")]

    touched, warnings = label_noise({PW_VAR: "pw"}, emails, [0, 1], archive=False)

    assert (touched, warnings) == (2, [])
    fake = factory.instances[0]
    assert fake.select_calls == [("INBOX", False)]  # read-write: STORE needs it
    assert fake.create_calls == ['"mailtriage/noise"']
    assert fake.store_calls == [
        ("501", "+X-GM-LABELS", '("mailtriage/noise")'),
        ("502", "+X-GM-LABELS", '("mailtriage/noise")'),
    ]


def test_archive_removes_only_the_inbox_label(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch)

    label_noise({PW_VAR: "pw"}, [make_email(0, uid="501")], [0], archive=True)

    assert factory.instances[0].store_calls == [
        ("501", "+X-GM-LABELS", '("mailtriage/noise")'),
        ("501", "-X-GM-LABELS", "(\\Inbox)"),
    ]


def test_only_the_given_indexes_are_touched_and_rule_protected_senders_never_are(monkeypatch: Any) -> None:
    """cli hands label_noise rules.omitted(): kept items and rule-named
    senders are not in it, so they can never be labeled or archived."""
    factory = _patch(monkeypatch)
    cfg = Config(
        delivery="email",
        rules={"always_ignore": [], "always_surface": ["@vip.com"], "always_action": ["boss@corp.com"]},
    )
    emails = [
        make_email(0, uid="1"),  # kept by the model
        make_email(1, uid="2"),  # noise
        make_email(2, uid="3", **{"from": "news@vip.com"}),  # rule-protected
        make_email(3, uid="4", **{"from": "Boss <boss@corp.com>"}),  # rule-protected
        make_email(4, uid=""),  # noise but synthetic (no uid) -- nothing to address
    ]
    idxs = omitted(cfg, emails, [make_triaged(0)])
    assert idxs == [1, 4]

    touched, _ = label_noise({PW_VAR: "pw"}, emails, idxs, archive=True)

    assert touched == 1
    assert [args[0] for args in factory.instances[0].store_calls] == ["2", "2"]


def test_nothing_to_do_never_connects(monkeypatch: Any) -> None:
    factory = _patch(monkeypatch)

    assert label_noise({PW_VAR: "pw"}, [make_email(0)], [], archive=True) == (0, [])
    assert factory.instances == []


def test_login_failure_and_missing_password_are_warnings(monkeypatch: Any) -> None:
    _patch(monkeypatch, login_error=OSError("login refused"))
    touched, warnings = label_noise({PW_VAR: "pw"}, [make_email(0)], [0])
    assert touched == 0 and "login refused" in warnings[0]["error"]

    touched, warnings = label_noise({}, [make_email(0)], [0])
    assert touched == 0 and PW_VAR in warnings[0]["error"]


# --- cli wiring: flags gate the write, dry-run never writes -----------------


def _run_with(monkeypatch: Any, noise: dict[str, bool], dry_run: bool) -> list[tuple[Any, ...]]:
    import mailtriage.cli as cli_module
    import mailtriage.delivery as delivery_module
    import mailtriage.triage as triage_module
    from tests.test_cli import _email, _triaged

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        cli_module, "pull", lambda environ, now, hours: {"messages": [_email(0), _email(1)], "warnings": []}
    )
    monkeypatch.setattr(
        cli_module, "enrich", lambda *a, **k: {"threads": 0, "fetches": 0, "senders": 0, "warnings": []}
    )
    monkeypatch.setattr(triage_module, "triage", lambda cfg, emails, now: [_triaged(0)])

    def fake_label_noise(env: Any, emails: Any, idxs: list[int], archive: bool = False) -> tuple[int, list[Any]]:
        calls.append((idxs, archive))
        return len(idxs), []

    monkeypatch.setattr(cli_module, "label_noise", fake_label_noise)
    monkeypatch.setattr(delivery_module, "send", lambda cfg, kept: None)
    cfg = Config(
        delivery="email",
        email_to="me@example.com",
        email_from="bot@example.com",
        carry_over=False,
        draft_replies=False,
        noise=noise,
    )
    cli_module.run(cfg, dry_run=dry_run)
    return calls


def test_cli_skips_noise_stage_unless_label_is_on(monkeypatch: Any) -> None:
    assert _run_with(monkeypatch, {"label": False, "archive": False}, dry_run=False) == []


def test_cli_labels_omitted_candidates_with_archive_flag(monkeypatch: Any) -> None:
    assert _run_with(monkeypatch, {"label": True, "archive": False}, dry_run=False) == [([1], False)]
    assert _run_with(monkeypatch, {"label": True, "archive": True}, dry_run=False) == [([1], True)]


def test_cli_never_writes_noise_labels_on_dry_run(monkeypatch: Any) -> None:
    assert _run_with(monkeypatch, {"label": True, "archive": True}, dry_run=True) == []
