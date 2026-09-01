"""Machine-checks for the cross-file contracts that break forks silently.

Three files must agree without ever importing each other: the wizard
(docs/index.html) writes secrets and config.yaml, the workflow
(.github/workflows/digest.yml) exports the secrets, and the engine reads both.
A one-character divergence produces no error anywhere — just a fork that
quietly stops working. These tests are the only thing standing between a
refactor and that outcome.
"""

import dataclasses
from pathlib import Path

from mailtriage.config import Config
from mailtriage.imap_pull import pw_env_var
from mailtriage.triage import PROVIDERS

ROOT = Path(__file__).resolve().parent.parent
WIZARD = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")


# --- the MAIL_PW_ slug mirror -------------------------------------------

# The exact JS transform the wizard uses. If this line changes in
# docs/index.html, it MUST still equal pw_env_var's behavior — update both
# sides and this pin together.
JS_TRANSFORM = '"MAIL_PW_" + email.toUpperCase().replace(/[^A-Z0-9]/g, "_")'


def test_wizard_slug_transform_is_the_pinned_mirror():
    assert JS_TRANSFORM in WIZARD, (
        "docs/index.html no longer contains the exact mailPwSlug transform. "
        "It must stay character-for-character equivalent to imap_pull.pw_env_var, "
        "or every secret the wizard writes gets a name the engine never reads."
    )


def test_python_side_of_the_mirror_is_pinned():
    # Pin the Python behavior the JS mirrors — including the awkward chars.
    assert pw_env_var("alice@gmail.com") == "MAIL_PW_ALICE_GMAIL_COM"
    assert pw_env_var("a.b+x@work.co") == "MAIL_PW_A_B_X_WORK_CO"


# --- config.yaml field names --------------------------------------------


def test_every_config_field_appears_in_wizard_and_shipped_yaml():
    shipped = (ROOT / "config.yaml").read_text(encoding="utf-8")
    for f in dataclasses.fields(Config):
        assert f.name in WIZARD, (
            f"Config field '{f.name}' is missing from docs/index.html — the wizard "
            "writes config.yaml, and a field it doesn't know about can never be set "
            "through the settings page."
        )
        assert f.name in shipped, (
            f"Config field '{f.name}' is missing from the committed config.yaml — "
            "the shipped default must exercise the full contract."
        )


# --- workflow filename + secret names -----------------------------------


def test_workflow_lives_at_the_literal_path_the_wizard_dispatches():
    assert (ROOT / ".github" / "workflows" / "digest.yml").is_file()
    assert 'const WORKFLOW = "digest.yml";' in WIZARD, (
        "The wizard dispatches the workflow by literal filename. Renaming "
        "digest.yml requires changing WORKFLOW in docs/index.html in the same commit."
    )


def test_secret_names_appear_in_wizard_and_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CODEX_AUTH_JSON",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "MAIL_ACCOUNTS",
        "RESEND_API_KEY",
    ):
        assert name in WIZARD, f"secret '{name}' missing from the wizard"
        assert name in readme, f"secret '{name}' missing from the README setup docs"


def test_all_five_engine_providers_appear_in_wizard():
    # The provider picker writes one of these literal strings as `provider:`
    # in config.yaml. Imported from the engine so a rename on either side
    # fails this test instead of silently forking the two.
    for name in PROVIDERS:
        assert name in WIZARD, (
            f"engine provider '{name}' (triage.PROVIDERS) missing from docs/index.html — "
            "the wizard's picker must write this exact string as `provider:`."
        )


# --- wizard hygiene ------------------------------------------------------


def test_wizard_has_no_external_asset_urls():
    # The page must work from file:// and keep its "everything in this tab"
    # privacy claim — no CDN scripts, no remote stylesheets.
    assert 'src="http' not in WIZARD
    assert "src='http" not in WIZARD


def test_wizard_never_persists_secrets_to_localstorage():
    # KEEP is the allowlist of field ids remembered between visits. Secret and
    # token field ids must never appear in it -- email addresses count now too,
    # since they live in EMAIL_TO/EMAIL_FROM secrets, not config.yaml.
    keep_line = next(line for line in WIZARD.splitlines() if "const KEEP" in line)
    for forbidden in ("token", "anthropic", "oauth", "resend", "pw", "email", "codex", "openai", "gemini"):
        assert forbidden not in keep_line.lower(), (
            f"localStorage KEEP list appears to persist a secret field ('{forbidden}' found in: {keep_line.strip()})"
        )


# --- no personal address ever committed ----------------------------------


def test_no_personal_email_address_anywhere_in_tracked_files():
    # This repo is public. A literal personal address here would leak it to
    # every fork, forever, in git history -- the whole point of this contract.
    # Built from parts so this file's own source text doesn't contain the
    # contiguous string it's searching for (grep for it would then always "hit").
    needle = "ashishbeerelli" + "1"
    globs = ["config.yaml", "README.md", "docs/index.html", "src/**/*.py", "tests/**/*.py"]
    hits = []
    for pattern in globs:
        for path in ROOT.glob(pattern):
            if path.is_file() and needle in path.read_text(encoding="utf-8"):
                hits.append(str(path.relative_to(ROOT)))
    assert not hits, f"personal address found in tracked files: {hits}"
