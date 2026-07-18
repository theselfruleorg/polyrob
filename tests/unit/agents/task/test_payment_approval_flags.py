"""Unit tests for the Task 9 / G-2 flag helpers: `payment_approval_mode()`,
`approval_grant_ttl_hours()`, and the `PAYMENT_APPROVAL_TOOLS` constant.

fix pass 1 (Finding 2): these flags are now FROZEN AT IMPORT (mirroring
`tools/controller/approval.py`'s WS-7 freeze of `APPROVAL_REQUIRED_TOOLS`/
`APPROVAL_PROVIDER`) rather than read live from `os.getenv` on every call — a
mid-process env mutation can no longer retarget money-critical gating. Every test
below that wants to observe an env value must explicitly call
`_refreeze_payment_approval_flags_for_tests()` AFTER monkeypatching, exactly like
the sibling flags' tests do.
"""
import pytest

from agents.task.constants import (
    PAYMENT_APPROVAL_TOOLS,
    _refreeze_payment_approval_flags_for_tests,
    approval_grant_ttl_hours,
    payment_approval_mode,
    payment_approval_timeout_sec,
)


@pytest.fixture(autouse=True)
def _clean_payment_flags(monkeypatch):
    """Baseline: no env set, frozen snapshot re-synced before and after every test
    so tests in this module (and any that ran before it) can't bleed into each
    other via the frozen module globals."""
    for k in ("PAYMENT_APPROVAL_MODE", "APPROVAL_GRANT_TTL_HOURS", "APPROVAL_TIMEOUT_SEC"):
        monkeypatch.delenv(k, raising=False)
    _refreeze_payment_approval_flags_for_tests()
    yield
    _refreeze_payment_approval_flags_for_tests()


def test_payment_approval_tools_includes_x402_request():
    assert "x402_request" in PAYMENT_APPROVAL_TOOLS


def test_payment_approval_tools_includes_namespaced_trade_verbs():
    """L9: live-trade order verbs are money-moving too — a within-cap live order
    should get the SAME owner-in-the-loop an invoice does. Container-tool actions
    register NAMESPACED, so these are the runtime names the gate matches."""
    for verb in (
        "hyperliquid_place_limit_order",
        "hyperliquid_place_market_order",
        "polymarket_place_limit_order",
        "polymarket_place_market_order",
    ):
        assert verb in PAYMENT_APPROVAL_TOOLS, verb


def test_mode_defaults_to_approve():
    assert payment_approval_mode() == "approve"


def test_mode_explicit_auto(monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    _refreeze_payment_approval_flags_for_tests()
    assert payment_approval_mode() == "auto"


def test_mode_explicit_approve(monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "approve")
    _refreeze_payment_approval_flags_for_tests()
    assert payment_approval_mode() == "approve"


def test_mode_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "AUTO")
    _refreeze_payment_approval_flags_for_tests()
    assert payment_approval_mode() == "auto"


def test_mode_unknown_value_falls_back_to_approve(monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "bogus")
    _refreeze_payment_approval_flags_for_tests()
    assert payment_approval_mode() == "approve"


def test_grant_ttl_default():
    assert approval_grant_ttl_hours() == 24.0


def test_grant_ttl_explicit(monkeypatch):
    monkeypatch.setenv("APPROVAL_GRANT_TTL_HOURS", "6")
    _refreeze_payment_approval_flags_for_tests()
    assert approval_grant_ttl_hours() == 6.0


def test_grant_ttl_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("APPROVAL_GRANT_TTL_HOURS", "not-a-number")
    _refreeze_payment_approval_flags_for_tests()
    assert approval_grant_ttl_hours() == 24.0


def test_payment_timeout_defaults_to_300_unset():
    """A money round-trip over Telegram needs minutes — the generic 30s default
    (tools/controller/approval.py) would time out almost every legitimate ask."""
    assert payment_approval_timeout_sec() == 300.0


def test_payment_timeout_explicit_env_wins(monkeypatch):
    monkeypatch.setenv("APPROVAL_TIMEOUT_SEC", "45")
    _refreeze_payment_approval_flags_for_tests()
    assert payment_approval_timeout_sec() == 45.0


def test_payment_timeout_invalid_falls_back_to_300(monkeypatch):
    monkeypatch.setenv("APPROVAL_TIMEOUT_SEC", "not-a-number")
    _refreeze_payment_approval_flags_for_tests()
    assert payment_approval_timeout_sec() == 300.0


# --- frozen-at-import contract (Finding 2) ----------------------------------------
# Mirrors tests/unit/tools/test_protected_config_guard.py's
# test_approval_flags_frozen_at_import / test_approval_flags_reflect_env_at_freeze_time.

def test_payment_flags_frozen_at_import(monkeypatch):
    _refreeze_payment_approval_flags_for_tests()  # baseline (no env)
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    monkeypatch.setenv("APPROVAL_GRANT_TTL_HOURS", "6")
    monkeypatch.setenv("APPROVAL_TIMEOUT_SEC", "45")
    # frozen snapshot must NOT reflect the post-import mutation (no refreeze call)
    assert payment_approval_mode() == "approve"
    assert approval_grant_ttl_hours() == 24.0
    assert payment_approval_timeout_sec() == 300.0


def test_payment_flags_reflect_env_at_freeze_time(monkeypatch):
    monkeypatch.setenv("PAYMENT_APPROVAL_MODE", "auto")
    monkeypatch.setenv("APPROVAL_GRANT_TTL_HOURS", "6")
    monkeypatch.setenv("APPROVAL_TIMEOUT_SEC", "45")
    _refreeze_payment_approval_flags_for_tests()
    assert payment_approval_mode() == "auto"
    assert approval_grant_ttl_hours() == 6.0
    assert payment_approval_timeout_sec() == 45.0
