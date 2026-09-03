"""delivery/strings.py is the digest's chrome in nine languages. The table
has to stay rectangular -- a key missing from one language is a digest that
renders a bare key at someone -- and `language` must never change anything
but the wording.
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

import pytest

from mailtriage.config import Config
from mailtriage.delivery import mail
from mailtriage.delivery.strings import FALLBACK, LANGUAGES, STRINGS, base_code, known, t
from mailtriage.models import Triaged


def _cfg(language: str = "en", **over: Any) -> Config:
    return Config(delivery="email", language=language, **over)


def _item(bucket: str = "needs_action", **over: Any) -> Triaged:
    base: Triaged = {
        "bucket": bucket,
        "note": "a note",
        "account": "me@example.com",
        "sender": "Alice <alice@example.com>",
        "subject": "a subject",
        "link": "https://mail.example.com/1",
        "date": "2026-08-28T00:00:00+00:00",
        "unread": False,
        "idx": 0,
        "draft": "",
    }
    return cast(Triaged, {**base, **over})


# --- the table ------------------------------------------------------------


def test_every_language_has_exactly_the_english_key_set():
    english = set(STRINGS[FALLBACK])
    for code in LANGUAGES:
        missing, extra = english - set(STRINGS[code]), set(STRINGS[code]) - english
        assert not missing, f"language {code!r} is missing keys: {sorted(missing)}"
        assert not extra, f"language {code!r} has keys English doesn't: {sorted(extra)}"


def test_the_nine_promised_languages_are_present():
    assert set(LANGUAGES) == {"en", "es", "fr", "de", "pt", "it", "nl", "hi", "ja"}


def test_no_string_is_left_untranslated_by_accident():
    """A translated row that still holds the English text is almost always a
    forgotten key. The genuinely-identical ones are listed here, per language,
    so a real omission can't hide behind a blanket allowance."""
    # Pure punctuation/format, or a proper noun ("Google Calendar").
    universal = {"section_group", "add_to_calendar"}
    same_on_purpose: dict[str, set[str]] = {
        "es": set(),
        "fr": {"msgs_one", "msgs_other", "minutes_one", "minutes_other"},  # real French words, same spelling
        "de": set(),
        "pt": set(),
        "it": set(),
        "nl": {"later", "count_open"},  # "later" and "open" are Dutch words too
        "hi": set(),
        "ja": set(),
    }
    for code in LANGUAGES:
        if code == FALLBACK:
            continue
        allowed = universal | same_on_purpose[code]
        copied = {k for k, v in STRINGS[code].items() if v == STRINGS[FALLBACK][k]} - allowed
        assert not copied, f"language {code!r} still has the English text for: {sorted(copied)}"


def test_every_placeholder_survives_translation():
    """A translation that drops or renames a {placeholder} would render the
    wrong thing (or raise) at digest time."""
    import re

    for code in LANGUAGES:
        for key, english in STRINGS[FALLBACK].items():
            want = set(re.findall(r"\{(\w+)\}", english))
            got = set(re.findall(r"\{(\w+)\}", STRINGS[code][key]))
            assert want == got, f"{code}.{key} has placeholders {sorted(got)}, English has {sorted(want)}"


# --- t() ------------------------------------------------------------------


def test_base_code_strips_region_and_case():
    assert base_code("pt-BR") == "pt"
    assert base_code(" JA ") == "ja"
    assert base_code("es_ES") == "es"
    assert known("pt-BR") and not known("klingon")


def test_t_translates_and_formats():
    assert t(_cfg("fr"), "needs_action") == "Action requise"
    assert t(_cfg("es"), "act_read", a=2, r=3) == "2 por hacer · 3 por leer"


def test_t_falls_back_to_english_for_an_unknown_language():
    assert t(_cfg("klingon"), "needs_action") == "Needs action"
    assert t(_cfg("pt-BR"), "needs_action") == "Requer ação"  # region tag still resolves


def test_t_picks_the_plural_form_by_n():
    assert t(_cfg(), "days", n=1) == "1 day"
    assert t(_cfg(), "days", n=3) == "3 days"
    assert t(_cfg("de"), "msgs", n=1) == "1 Nachricht"
    assert t(_cfg("de"), "msgs", n=2) == "2 Nachrichten"


def test_t_returns_the_key_for_a_key_nobody_has():
    assert t(_cfg(), "no_such_key") == "no_such_key"


def test_t_survives_a_translation_with_a_stray_brace(capsys, monkeypatch):
    monkeypatch.setitem(STRINGS["en"], "broken", "a {nope} brace")
    assert t(_cfg(), "broken") == "a {nope} brace"
    assert "bad format in string" in capsys.readouterr().err


# --- config ---------------------------------------------------------------


def test_unknown_language_warns_but_never_fails(capsys):
    cfg = Config.from_mapping({"delivery": "email", "language": "klingon"})
    assert cfg.language == "klingon"  # kept verbatim, not silently rewritten
    err = capsys.readouterr().err
    assert "no translation for language 'klingon'" in err
    assert "en, es, fr, de, pt, it, nl, hi, ja" in err


def test_known_language_is_silent(capsys):
    Config.from_mapping({"delivery": "email", "language": "ja"})
    assert capsys.readouterr().err == ""


def test_language_defaults_to_english():
    assert Config.from_mapping({"delivery": "email"}).language == "en"


# --- the rendered digest --------------------------------------------------


def test_digest_chrome_is_translated_end_to_end():
    html = mail.email_html(
        _cfg("de", label="mailtriage/action"),
        [_item(due="2026-09-01"), _item("carried"), _item("worth_reading")],
        today=date(2026, 9, 3),
    )
    for german in ("Erfordert Handeln", "Überfällig", "Wartet weiter auf Sie", "Lesenswert", "zu erledigen"):
        assert german in html
    for english in ("Needs action", "Still waiting on you", "Worth reading", "to act ·"):
        assert english not in html


def test_weekly_chrome_is_translated_end_to_end():
    week: Any = {
        "accounts": {
            "me@example.com": {
                "replied": [],
                "archived": [],
                "open": [
                    {
                        "account": "me@example.com",
                        "sender": "Boss <boss@corp.com>",
                        "subject": "open one",
                        "date": "2026-08-25T00:00:00+00:00",
                        "link": "https://mail.example.com/1",
                        "age_days": 3,
                    }
                ],
            }
        },
        "warnings": [],
    }
    html = mail.weekly_html(_cfg("fr"), week, done_count=2, totals={"triaged": 5, "drafts": 1, "minutes": 12})
    assert "Votre semaine" in html and "2 marqués comme faits" in html
    assert "a traité 5 messages et rédigé 1 réponse" in html
    assert "Your week" not in html


def test_language_changes_only_the_wording_not_the_structure():
    """Item numbering, links and section ORDER must be identical in every
    language -- a reply saying "done 2" has to mean the same item."""
    items = [_item(), _item("carried"), _item("worth_reading")]
    en = mail.email_html(_cfg("en"), items, today=date(2026, 9, 3))
    ja = mail.email_html(_cfg("ja"), items, today=date(2026, 9, 3))
    for n in (">#1</a>", ">#2</a>", ">#3</a>"):
        assert n in en and n in ja
    assert en.index(">#1</a>") < en.index(">#2</a>") < en.index(">#3</a>")
    assert ja.index(">#1</a>") < ja.index(">#2</a>") < ja.index(">#3</a>")


def test_section_keys_stay_english_whatever_the_language():
    """digest_groups' keys back reply handling and the tests; only
    section_heading() is language-dependent."""
    groups = mail.digest_groups([_item(due="2026-09-01"), _item("carried")], date(2026, 9, 3))
    assert [k for _kind, k, _i in groups] == ["needs_action:overdue", "still_waiting"]
    assert mail.section_heading(_cfg("es"), "needs_action:overdue") == "Requiere acción · Atrasado"


def test_commands_hint_stays_english_because_it_quotes_literal_commands():
    """The label names and reply words in the hint are what the engine
    actually parses -- translating them would tell the reader to type
    something no run understands."""
    html = mail.email_html(_cfg("ja"), [_item()])
    for literal in ("mailtriage/done", "mailtriage/snooze-3d", '"done 2"'.replace('"', "&quot;")):
        assert literal in html


@pytest.mark.parametrize("code", ["en", "es", "fr", "de", "pt", "it", "nl", "hi", "ja"])
def test_every_language_renders_a_digest_and_a_review_without_raising(code):
    week: Any = {"accounts": {"me@example.com": {"replied": [], "archived": [], "open": []}}, "warnings": []}
    assert mail.email_html(_cfg(code), [_item(due="2026-09-01"), _item("carried")], today=date(2026, 9, 3))
    assert mail.weekly_html(_cfg(code), week, done_count=1, totals={"triaged": 1, "drafts": 1, "minutes": 1})
