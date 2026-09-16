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

The families are pinned by ENUMERATING the flags catalog, not by listing names,
so a `WALLET_*_USD` cap added tomorrow is covered without editing this file.
"""
import pytest

from core import config_service
from core.config_service import is_console_unwritable
from core.flags import is_secret_flag
from core.flags_catalog import CATALOG

# Concrete-name rows only; a dynamic "<...>" pattern row is not a writable key.
FLAG_NAMES = sorted({n for n, _g, _d, _desc in CATALOG if "<" not in n})


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
                     "WEBVIEW_READ_ONLY", "WEBVIEW_AUTH_ENABLED", "WEBVIEW_HOST",
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
    "POLYROB_OWNER_EMAIL", "BOT_OWNER_EMAIL", "POLYROB_REQUIRE_PAIRING",
    "WALLET_DAILY_CAP_USD", "DEFI_AUTONOMOUS_MAX_USD", "X402_INVOICE_MAX_USD",
    "X402_INVOICE_DAILY_MAX", "AGENT_WALLET_MASTER_SEED", "AGENT_WALLET_NETWORK",
    "DEFI_SOLANA_RPC", "DEFI_EVM_RPC_BASE", "BASE_RPC_URL",
    "PAYMENT_APPROVAL_MODE", "APPROVAL_PROVIDER", "APPROVAL_REQUIRED_TOOLS",
    "DEFAULT_APPROVAL_REQUIRED_TOOLS", "TWITTER_REQUIRE_APPROVAL",
    "POLYROB_LOCAL", "AUTONOMY_MODE", "AUTONOMY_POSTURE", "AGENT_COMPUTE_POSTURE",
    "POLYROB_POSTURE", "WEBVIEW_READ_ONLY", "WEBVIEW_AUTH_ENABLED",
    "WEBGATE_MULTITENANT", "WEBGATE_HOST", "DELEGATE_BLOCKED_TOOLS",
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


def test_legacy_credential_set_is_still_covered():
    for key in sorted(config_service.CONSOLE_UNWRITABLE_FLAGS):
        assert is_console_unwritable(key), key


def test_local_surface_still_writes_them(monkeypatch):
    """The refusal is about the SURFACE, not the flag: the local CLI keeps
    writing every one of these (that is the documented remedy)."""
    sentinel = config_service.SetResult(True, "written", "ok")
    monkeypatch.setattr(config_service, "_set_flag", lambda *a, **k: sentinel)
    assert config_service.set_value("WALLET_DAILY_CAP_USD", "5") is sentinel


@pytest.mark.parametrize("key", ["GOALS_ENABLED", "CURATOR_INTERVAL_HOURS",
                                 "WEBVIEW_FEED_DEFAULT_LIMIT",
                                 "LLM_MAX_OUTPUT_TOKENS", "CHAT_PATH_LINKS"])
def test_ordinary_flags_stay_console_writable(key):
    """Over-blocking has a cost too — the console's config page is a real owner
    seat. An ordinary operational knob must stay writable."""
    assert not is_console_unwritable(key), key


@pytest.mark.parametrize("key", ["goals.daily_quota", "budget.wallet_daily_usd",
                                 "style.verbosity"])
def test_preferences_are_not_env_flags(key):
    """Typed preferences have their OWN trust ladder (guarded ⇒ queued for owner
    review). The env-flag denylist must not swallow them — `budget.wallet_daily_usd`
    reads money-shaped but is a pref key, not an env flag."""
    assert not is_console_unwritable(key), key
