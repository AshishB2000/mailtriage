"""Machine-checks for the cross-file contracts that break forks silently.

Three files must agree without ever importing each other: the wizard
(docs/index.html) writes secrets and config.yaml, the workflow
(.github/workflows/digest.yml) exports the secrets, and the engine reads both.
A one-character divergence produces no error anywhere — just a fork that
quietly stops working. These tests are the only thing standing between a
refactor and that outcome.
"""

import dataclasses
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from mailtriage.config import DELIVERIES, Config
from mailtriage.delivery import BACKENDS, BACKENDS_HTML
from mailtriage.imap_pull import legacy_pw_env_var, pw_env_var
from mailtriage.schedule import max_gap_hours
from mailtriage.triage import PROVIDERS

ROOT = Path(__file__).resolve().parent.parent
WIZARD = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")


# --- the MAIL_PW_ name mirror --------------------------------------------

# The exact JS transform the wizard uses. If this line changes in
# docs/index.html, it MUST still equal pw_env_var's behavior — update both
# sides and this pin together. libsodium's crypto_generichash is unkeyed
# BLAKE2b, so at digest size 16 it equals hashlib.blake2b(digest_size=16).
JS_TRANSFORM = (
    '"MAIL_PW_" + sodium.to_hex(sodium.crypto_generichash(16, '
    "sodium.from_string(email.trim().toLowerCase()))).slice(0, 16).toUpperCase()"
)
# One hard-coded vector, checked against BOTH sides: pw_env_var must produce
# it, and docs/index.html must carry it verbatim as a `// vector:` comment
# next to mailPwSlug. Drift on either side fails here instead of in a fork.
VECTOR_ADDR, VECTOR_NAME = "alice@gmail.com", "MAIL_PW_F24FE3C393F64986"


def test_wizard_slug_transform_is_the_pinned_mirror():
    assert JS_TRANSFORM in WIZARD, (
        "docs/index.html no longer contains the exact mailPwSlug transform. "
        "It must stay character-for-character equivalent to imap_pull.pw_env_var, "
        "or every secret the wizard writes gets a name the engine never reads."
    )


def test_wizard_vector_comment_matches_the_engine():
    m = re.search(r"^// vector: (\S+) -> (\S+)$", WIZARD, re.MULTILINE)
    assert m, "docs/index.html is missing the `// vector: <addr> -> <name>` comment next to mailPwSlug"
    assert (m.group(1), m.group(2)) == (VECTOR_ADDR, VECTOR_NAME)
    assert pw_env_var(m.group(1)) == m.group(2)


def test_python_side_of_the_mirror_is_pinned():
    assert pw_env_var(VECTOR_ADDR) == VECTOR_NAME
    assert pw_env_var("a.b+x@work.co") == "MAIL_PW_5335BF4B59240EFC"
    # Deprecated pre-hash name: still read by the engine, never written.
    assert legacy_pw_env_var("a.b+x@work.co") == "MAIL_PW_A_B_X_WORK_CO"


# --- the max_gap_hours (window_hours auto-compute) mirror ---------------

# The exact JS expression the wizard's maxGapHours() returns from. If this
# line changes in docs/index.html, it MUST still be equivalent to
# mailtriage.schedule.max_gap_hours -- the wizard uses it to auto-compute
# window_hours from run_at, and a divergent gap calculation would either
# under-cover the schedule (silently dropped mail) or over-report it.
JS_GAP_EXPR = (
    "return Math.max.apply(null, slots.map((s, i) => "
    "(((slotMinutes(slots[(i + 1) % slots.length]) - slotMinutes(s)) % 1440 + 1440) % 1440) / 60));"
)


def test_wizard_gap_expression_is_the_pinned_mirror():
    assert JS_GAP_EXPR in WIZARD, (
        "docs/index.html no longer contains the exact maxGapHours return expression. "
        "It must stay equivalent to mailtriage.schedule.max_gap_hours, or the wizard's "
        "auto-computed window_hours can drift from what the engine actually needs."
    )


def test_python_side_of_the_gap_mirror_is_pinned():
    assert max_gap_hours(["08:00", "18:00"]) == 14
    assert max_gap_hours(["08:00"]) == 24
    assert max_gap_hours(["18:00", "08:00"]) == 14
    assert max_gap_hours(["08:00", "14:00", "20:00"]) == 12
    assert max_gap_hours(["08:00", "12:30", "18:00"]) == 14


# --- forward-compat: carry_over / label (landing on a sibling branch) ---


def test_wizard_writes_carry_over_and_label_forward_compat():
    # config.py on this branch doesn't have these fields yet (a sibling
    # branch adds them with these exact names/defaults), so
    # test_every_config_field_appears_in_wizard_and_shipped_yaml above can't
    # see them. Pin them here so the wizard doesn't regress once that branch
    # lands.
    for name in ("carry_over", "label", "run_at", "timezone", "weekly_review", "draft_style", "rules", "accounts"):
        assert name in WIZARD, f"'{name}' missing from docs/index.html"


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
        "GEMINI_OAUTH_JSON",
        "MAIL_ACCOUNTS",
        "RESEND_API_KEY",
        "CALENDAR_ICS_URL",
    ):
        assert name in WIZARD, f"secret '{name}' missing from the wizard"
        assert name in readme, f"secret '{name}' missing from the README setup docs"


def test_wizard_delivery_picker_writes_both_modes():
    # buildYaml() must be able to emit either delivery mode -- a hardcoded
    # "delivery: email" (or the reverse) would silently flip every fork's
    # delivery choice the next time someone re-runs the wizard.
    assert "delivery: gmail" in WIZARD, "wizard is missing the 'delivery: gmail' literal"
    assert "delivery: email" in WIZARD, "wizard is missing the 'delivery: email' literal"
    assert "RESEND_API_KEY" in WIZARD


def test_all_engine_providers_appear_in_wizard():
    # The provider picker writes one of these literal strings as `provider:`
    # in config.yaml. Imported from the engine so a rename on either side
    # fails this test instead of silently forking the two.
    for name in PROVIDERS:
        assert name in WIZARD, (
            f"engine provider '{name}' (triage.PROVIDERS) missing from docs/index.html — "
            "the wizard's picker must write this exact string as `provider:`."
        )


# --- delivery channels ---------------------------------------------------

DELIVERY_SECRETS = ("TELEGRAM_BOT_TOKEN", "SLACK_WEBHOOK_URL", "DISCORD_WEBHOOK_URL", "NTFY_TOPIC_URL")


def test_every_delivery_value_has_both_backends_and_a_wizard_radio():
    # Config validates against the Literal; the dispatch tables and the
    # wizard's picker must agree, or a delivery can be half-added.
    assert set(BACKENDS) == set(BACKENDS_HTML) == set(DELIVERIES)
    for name in DELIVERIES:
        assert f'name="delivery" value="{name}"' in WIZARD, f"wizard delivery picker is missing '{name}'"


def test_delivery_secret_names_appear_in_wizard_readme_and_claude_md():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claude_md = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for name in DELIVERY_SECRETS:
        assert name in WIZARD, f"secret '{name}' missing from the wizard"
        assert name in readme, f"secret '{name}' missing from README"
        assert name in claude_md, f"secret '{name}' missing from CLAUDE.md"


# --- profiles survive the wizard -----------------------------------------

YAML_SECTION = "/* ------------------------------------------------------------ yaml */"
YAML_SECTION_END = "/* ------------------------------------------------------------ step 1 */"

PROFILES_BLOCK = """profiles:
  # the work one goes to Slack
  work:
    accounts: ["me@corp.com"]
    delivery: slack
    run_at: ["09:00", "17:00"]
    interests: |
      Anything from the eng-leads list.
  home:
    accounts: ["me@gmail.com"]
"""


def test_wizard_carries_profiles_block_by_string_inspection():
    # The wizard has no UI for profiles; buildYaml must still write the
    # block back (verbatim, from profilesBlock) or a save silently drops it.
    assert "function profilesBlock(" in WIZARD
    assert "profiles_raw" in WIZARD
    assert "S.profilesRaw = profilesBlock(" in WIZARD


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_wizard_round_trips_profiles_through_node(tmp_path):
    """Run the wizard's own (pure) YAML helpers under node: parse the shipped
    config.yaml with a hand-edited profiles block, rebuild it the way a save
    does, and check the engine reads the same profiles back."""
    js = WIZARD[WIZARD.index(YAML_SECTION) : WIZARD.index(YAML_SECTION_END)]
    shipped = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert "profiles: {}" in shipped
    sample = tmp_path / "config.yaml"
    sample.write_text(shipped.replace("profiles: {}\n", PROFILES_BLOCK), encoding="utf-8")
    driver = tmp_path / "driver.js"
    driver.write_text(
        js
        + """
const text = require("fs").readFileSync(process.argv[2], "utf8");
const cfg = parseConfig(text);
process.stdout.write(buildYaml({
  interests: cfg.interests, avoid: cfg.avoid, reading_count: cfg.reading_count, window_hours: cfg.window_hours,
  run_at: cfg.run_at, timezone: cfg.timezone, weekly_review: cfg.weekly_review, delivery: cfg.delivery,
  provider: "claude-api", draft_replies: cfg.draft_replies, draft_style: cfg.draft_style, rules: cfg.rules,
  accounts: {}, carry_over: cfg.carry_over, label: cfg.label, telegram_chat_id: cfg.telegram_chat_id,
  digest_format: cfg.digest_format, nag_after_days: cfg.nag_after_days, profiles_raw: profilesBlock(text)
}));
""",
        encoding="utf-8",
    )
    out = subprocess.run(["node", str(driver), str(sample)], capture_output=True, text=True, check=True).stdout

    rebuilt = yaml.safe_load(out)
    assert rebuilt["profiles"] == yaml.safe_load(PROFILES_BLOCK)["profiles"]
    assert "# the work one goes to Slack" in out  # verbatim, comments included
    cfg = Config.from_mapping(rebuilt)
    assert cfg.profile("work").delivery == "slack"
    assert cfg.profile("work").run_at == ["09:00", "17:00"]
    assert cfg.profile("home").delivery == cfg.delivery

    # and a config with no profiles rebuilds as the empty mapping
    sample.write_text(shipped, encoding="utf-8")
    out = subprocess.run(["node", str(driver), str(sample)], capture_output=True, text=True, check=True).stdout
    assert yaml.safe_load(out)["profiles"] == {}


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
    for forbidden in (
        "token",
        "anthropic",
        "oauth",
        "resend",
        "pw",
        "email",
        "codex",
        "openai",
        "gemini",
        "hook",
        "ntfy",
        "tg",
        "ics",
    ):
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


# --- dashboard: dispatch modes + workflow filenames ------------------------


def _wizard_const_list(name: str) -> list[str]:
    m = re.search(rf"^const {name} = \[([^\]]*)\];", WIZARD, re.MULTILINE)
    assert m, f"docs/index.html is missing `const {name} = [...]`"
    return re.findall(r'"([^"]+)"', m.group(1))


def test_wizard_modes_match_digest_yml_dispatch_input():
    # The dashboard's Run now / Send weekly review / Run doctor buttons send
    # `inputs: {mode}` to digest.yml. Both sides must list the same values in
    # the same order, or a button dispatches a mode the workflow rejects (422).
    workflow = (ROOT / ".github" / "workflows" / "digest.yml").read_text(encoding="utf-8")
    m = re.search(r"^\s+mode:\n(?:.*\n)*?\s+options: \[([^\]]*)\]", workflow, re.MULTILINE)
    assert m, "digest.yml has no workflow_dispatch `mode` input with an options list"
    assert (
        [o.strip() for o in m.group(1).split(",")]
        == _wizard_const_list("MODES")
        == ["digest", "weekly", "doctor", "bench"]
    )
    for mode in ("digest", "weekly", "doctor", "bench"):
        assert f'data-mode="{mode}"' in WIZARD, f"dashboard has no button for mode {mode!r}"


def test_sync_workflow_lives_at_the_literal_path_the_dashboard_dispatches():
    assert (ROOT / ".github" / "workflows" / "upstream-sync.yml").is_file()
    assert 'const SYNC_WORKFLOW = "upstream-sync.yml";' in WIZARD


def test_dashboard_reads_the_sample_digest_as_a_sibling_file():
    # The preview iframe must stay a relative sibling so it works from file://
    # and GitHub Pages alike; tests/test_sample.py keeps the file itself fresh.
    assert 'data-src="sample-digest.html"' in WIZARD
    assert (ROOT / "docs" / "sample-digest.html").is_file()


def test_dashboard_never_hardcodes_main_for_the_fork():
    # Every ref the page sends for the user's fork must be the fork's default
    # branch. The one literal "main" allowed is upstream's, in the compare URL
    # and the CHANGELOG link.
    for line in WIZARD.splitlines():
        if "ref:" in line and "default_branch" not in line:
            raise AssertionError(f"dispatch/commit ref not using S.repo.default_branch: {line.strip()}")
