"""S8 (agent + wallet security evaluation, 2026-09-14): a remote config-write
surface must not be able to hand the instance over.

`PATCH /api/webgate/config/{key}` writes `Path.cwd()/.polyrob/.env`, and
`polyrob.service` loads that file AFTER `/etc/polyrob/polyrob.env` — so a
console write OUTRANKS the operator's own env file on the next restart. Four
families must therefore never be writable from a remote surface:

  owner binding  — who the agent obeys (`ALLOWED_TELEGRAM_USER_IDS`, …)
  money bounds   — the caps/venues/endpoints the spend guard measures against
  approval       — whether a money verb needs an owner tap at all
  posture/trust  — `POLYROB_LOCAL`, `AUTONOMY_MODE`, the console's own gating

…plus every secret-shaped flag (`is_secret_flag`), which includes the wallet
master seed.

Since 2026-10-10 the runtime reads each flag's CLASS from the Access column of
docs/CONFIGURATION.md (`core.flags_catalog.ACCESS`), never a name token. The
family predicates below are a LINT over that column: a `WALLET_*_USD` cap added
tomorrow and classed `console` by mistake fails here.
"""
import pytest

from core import config_service
from core.config_service import is_console_unwritable
from core.flags import is_secret_flag
from core.flags_catalog import CATALOG

# Concrete-name rows only; a dynamic "<...>" pattern row is not a writable key.
FLAG_NAMES = sorted({n for n, *_ in CATALOG if "<" not in n})


def _segs(name):
    return name.split("_")


def _owner_binding(n):
    return ("OWNER" in _segs(n) or "PAIRING" in _segs(n)
            or "ALLOWLIST" in n or n.startswith("ALLOWED_"))


def _money_bound(n):
    segs = _segs(n)
    return ("WALLET" in segs or "USD" in segs or "CAP" in segs
            or "RPC" in segs or "TREASURY" in segs
            or (n.startswith("X402_") and "MAX" in segs))


def _approval(n):
    return "APPROVAL" in _segs(n)


def _posture_trust(n):
    return (n.startswith(("WEBGATE_", "CODE_EXEC_"))
            or "POSTURE" in _segs(n)
            or n in {"POLYROB_LOCAL", "POLYROB_LOCAL_OWNER", "AUTONOMY_MODE",
                     "WEBVIEW_READ_ONLY", "WEBVIEW_HOST",
                     "DELEGATE_BLOCKED_TOOLS", "SELF_ENV_ENABLED",
                     "SHELL_TOOLS_ENABLED"})


FAMILIES = {
    "owner-binding": _owner_binding,
    "money-bounds": _money_bound,
    "approval": _approval,
    "posture-trust": _posture_trust,
    "secret": is_secret_flag,
}

# Named verbatim in the S8 finding / the remediation list — these must be
# refused even if the family predicates above are later loosened.
REVIEW_NAMED = (
    "ALLOWED_TELEGRAM_USER_IDS", "POLYROB_OWNER_TELEGRAM_ID", "TELEGRAM_OWNER_ID",
    "POLYROB_OWNER_EMAIL", "POLYROB_REQUIRE_PAIRING",
    "WALLET_DAILY_CAP_USD", "DEFI_AUTONOMOUS_MAX_USD", "X402_INVOICE_MAX_USD",
    "X402_INVOICE_DAILY_MAX", "AGENT_WALLET_MASTER_SEED", "AGENT_WALLET_NETWORK",
    "DEFI_SOLANA_RPC", "DEFI_EVM_RPC_BASE", "BASE_RPC_URL",
    "PAYMENT_APPROVAL_MODE", "APPROVAL_PROVIDER", "APPROVAL_REQUIRED_TOOLS",
    "DEFAULT_APPROVAL_REQUIRED_TOOLS", "TWITTER_REQUIRE_APPROVAL",
    "POLYROB_LOCAL", "AUTONOMY_MODE", "AUTONOMY_POSTURE", "AGENT_COMPUTE_POSTURE",
    "POLYROB_POSTURE", "WEBVIEW_READ_ONLY",
    "WEBGATE_HOST", "DELEGATE_BLOCKED_TOOLS",
    "CODE_EXEC_ENABLED", "CODE_EXEC_BACKEND", "SELF_ENV_ENABLED",
    "SHELL_TOOLS_ENABLED",
)


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_every_catalogued_flag_in_the_family_is_refused(family):
    predicate = FAMILIES[family]
    covered = [n for n in FLAG_NAMES if predicate(n)]
    assert covered, f"{family}: predicate matched no catalog flag — stale test"
    missed = [n for n in covered if not is_console_unwritable(n)]
    assert not missed, (
        f"{family}: console-writable flags that can hand over the instance: {missed}")


@pytest.mark.parametrize("key", REVIEW_NAMED)
def test_review_named_flags_are_refused(key):
    assert is_console_unwritable(key), key


@pytest.mark.parametrize("key", REVIEW_NAMED)
def test_set_value_refuses_them_on_a_remote_surface(key):
    res = config_service.set_value(key, "x", surface="console")
    assert res.ok is False, key
    assert res.outcome == "refused", key
    assert "local CLI" in res.message, key


# 024 §2.6/§7.5: where inference goes or where credentials live.
CREDENTIAL_SURFACE = ("LLM_CUSTOM_PROVIDERS", "POLYROB_AUTH_STORE",
                      "LLM_AUTH_STORE_ENABLED", "LLM_CREDENTIAL_BORROW",
                      "MEMORY_SCOPES_ENABLED")


def test_legacy_credential_set_is_still_covered():
    for key in CREDENTIAL_SURFACE:
        assert is_console_unwritable(key), key


def test_every_catalog_row_declares_an_access_class():
    from core.flags_catalog import ACCESS
    names = {n for n, *_ in CATALOG}
    assert set(ACCESS) == names
    assert set(ACCESS.values()) <= {"console", "file", "secret"}


def test_class_comes_from_the_catalog_not_the_name(monkeypatch):
    """A flag's protection is its declared class: flipping the catalog entry
    flips the answer, whatever the name says."""
    from core import flags_catalog
    monkeypatch.setitem(flags_catalog.ACCESS, "WALLET_DAILY_CAP_USD", "console")
    assert not is_console_unwritable("WALLET_DAILY_CAP_USD")
    monkeypatch.setitem(flags_catalog.ACCESS, "GOALS_ENABLED", "file")
    assert is_console_unwritable("GOALS_ENABLED")
    monkeypatch.setitem(flags_catalog.ACCESS, "GOALS_ENABLED", "secret")
    assert is_secret_flag("GOALS_ENABLED")


def test_unknown_flag_shaped_key_fails_closed():
    assert is_console_unwritable("SOME_FLAG_NOBODY_DOCUMENTED")


def test_pattern_rows_carry_their_class():
    assert is_console_unwritable("OWNER_TELEGRAM_ID")          # OWNER_<SURFACE>_ID
    assert is_secret_flag("DEFI_EVM_RPC_ARBITRUM")             # DEFI_EVM_RPC_<CHAIN>
    assert not is_console_unwritable("POLYROB_OPENROUTER_MODEL")


def test_local_surface_still_writes_them(monkeypatch):
    """The refusal is about the SURFACE, not the flag: the local CLI keeps
    writing every one of these (that is the documented remedy)."""
    sentinel = config_service.SetResult(True, "written", "ok")
    monkeypatch.setattr(config_service, "_set_flag", lambda *a, **k: sentinel)
    assert config_service.set_value("WALLET_DAILY_CAP_USD", "5") is sentinel


@pytest.mark.parametrize("key", ["TOOL_AVAILABILITY_HINT", "CURATOR_INTERVAL_HOURS",
                                 "WEBVIEW_FEED_DEFAULT_LIMIT",
                                 "LLM_MAX_OUTPUT_TOKENS", "CHAT_PATH_LINKS",
                                 # caught by a WEB-4 trust word by accident
                                 "EMAIL_IMAP_POLL_SEC",
                                 "REQUIRE_SCHEMA_KEYS", "OUTBOUND_QUEUE_ENABLED",
                                 # natural-work sweep 2026-10-08
                                 "LLM_OUTAGE_NOTICE", "BILLING_FAILOVER_ENABLED",
                                 "TX_NOTIFY_SENT_HOLD_SEC", "POLYROB_SUPPORT_URL",
                                 "POLYROB_BRAND_URL", "POLYROB_ORG_URL",
                                 "POLYROB_TERMS_URL", "POLYROB_PRIVACY_URL",
                                 "THINK_SCRUBBER_ENABLED",
                                 "ANTHROPIC_DEFERRED_TOOLS", "STABLE_AUTONOMOUS_TOOLSET"])
def test_ordinary_flags_stay_console_writable(key):
    """Over-blocking has a cost too — the console's config page is a real owner
    seat. An ordinary operational knob must stay writable."""
    assert not is_console_unwritable(key), key


@pytest.mark.parametrize("key", ["GOALS_ENABLED", "WAKE_CHANGE_GATE", "POLYROB_OVERRIDES"])
def test_posture_members_and_their_override_line_are_file_only(key):
    """D7 (owner decision 2026-10-10): a posture member changes only through
    POLYROB_OVERRIDES, which is Access `file` — never a console write."""
    assert is_console_unwritable(key), key


@pytest.mark.parametrize("key", ["goals.daily_quota", "budget.wallet_daily_usd",
                                 "style.verbosity"])
def test_preferences_are_not_env_flags(key):
    """Typed preferences have their OWN trust ladder (guarded ⇒ queued for owner
    review). The env-flag denylist must not swallow them — `budget.wallet_daily_usd`
    reads money-shaped but is a pref key, not an env flag."""
    assert not is_console_unwritable(key), key


@pytest.mark.parametrize("key", ["GMAIL_IMAP_SERVER", "EXTERNAL_WRITE_PAUSE_GATE",
                                 "OUTBOUND_DOMAINS", "MEMORY_REQUIRE_USER_ID",
                                 # neighbours of the 2026-10-08 exemptions
                                 "WEB_FETCH_ALLOW_PRIVATE_URLS", "HISTORY_SECRET_SCRUB",
                                 "POLYROB_TOOL_DENYLIST", "X402_FACILITATOR_URL",
                                 "WALLET_DAILY_CAP_USD", "AUTONOMOUS_RIG_DEFAULT"])
def test_operational_exemption_leaves_trust_neighbours_refused(key):
    """The four exempt knobs share a word with these trust flags; the
    exemption is by exact name, so the trust flags stay file-only (WEB-4)."""
    assert is_console_unwritable(key), key
