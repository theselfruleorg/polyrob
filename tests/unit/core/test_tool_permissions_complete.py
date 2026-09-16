"""TOOL_PERMISSIONS completeness (043 A44 / A9, 2026-09-14).

Before this: TOOL_PERMISSIONS covered 17 of 36 capability-classified ids, so 19
tools were invisible to `high_risk_tool_ids()`/`medium_risk_tool_ids()` (the
catalog's risk-tier derivation) and to the future Capabilities tab — a tool
could be `money`/`high_impact`/`delegate_blocked` and STILL show no permissions
or risk tier anywhere product-facing.

Note on CATALOG_ALIASES direction: TOOL_PERMISSIONS is keyed by DESCRIPTOR name
(e.g. "browser_manager"); TOOL_CAPABILITIES is keyed by capability-table id
(e.g. "browser"). CATALOG_ALIASES maps descriptor -> capability id, so checking
completeness from the capability side requires applying the alias to the
TOOL_PERMISSIONS keys (not the capability ids) — mirrors
test_tool_capabilities.py::test_every_permissions_key_is_classified, which
does the same translation for the existing reverse-direction check.
"""
from core.tool_capabilities import (
    CATALOG_ALIASES,
    TOOL_CAPABILITIES,
    TOOL_PERMISSIONS,
)

# The only permission vocabulary in use today (collected from the pre-existing 17
# rows) — the brief's "reuse the existing vocabulary only" constraint, enforced.
_EXISTING_VOCABULARY = frozenset({
    "fs.read", "fs.write",
    "memory.read", "memory.write",
    "browser.control",
    "network.read", "network.write",
    "social.post",
    "wallet.spend",
    "email.send",
    "mcp.call",
    "process.spawn",
    "trade.execute",
})


def test_every_capability_id_has_a_permissions_row():
    """Every classified tool_id must be reachable through TOOL_PERMISSIONS,
    accounting for the descriptor<->capability naming dual (CATALOG_ALIASES)."""
    permission_ids = {CATALOG_ALIASES.get(k, k) for k in TOOL_PERMISSIONS}
    missing = set(TOOL_CAPABILITIES) - permission_ids
    assert not missing, f"add a TOOL_PERMISSIONS row for: {sorted(missing)}"


def test_no_permission_token_outside_the_existing_vocabulary():
    for tool_id, perms in TOOL_PERMISSIONS.items():
        unknown = set(perms) - _EXISTING_VOCABULARY
        assert not unknown, f"{tool_id}: permission token(s) outside the existing vocabulary: {sorted(unknown)}"


def test_the_19_previously_missing_ids_now_have_rows():
    """Pins the exact set the 2026-09-14 capability inventory (F-TOOL_PERMISSIONS-gap)
    named as missing, so a future rename/removal is caught explicitly."""
    previously_missing = {
        "knowledge", "defi_data", "web_fetch", "goal", "cronjob",
        "code_execution", "coding", "shell", "process", "self_env",
        "git", "github", "hf_deploy", "publish", "app_service", "tool_manage",
        "x402_pay", "x402_invoice", "defi_trade",
    }
    assert previously_missing <= set(TOOL_PERMISSIONS)


# The 19 previously-missing ids, split by the tier rule Controller decision #4
# named: money/exec -> high; high_impact-only -> medium; empty capability -> low.
# Scoped to exactly these 19 (not asserted as a whole-table invariant): a few
# PRE-EXISTING rows (email, twitter) are high_impact-only in TOOL_CAPABILITIES
# yet already land HIGH via a genuine external-write permission
# (email.send/social.post) — a legitimate, older exception to the simple rule,
# not something this task revisits.
_NEW_HIGH = frozenset({
    "code_execution", "coding", "shell", "process", "self_env",
    "x402_pay", "x402_invoice", "defi_trade",
})
_NEW_MEDIUM = frozenset({
    "goal", "cronjob", "git", "github", "hf_deploy", "publish",
    "app_service", "tool_manage", "web_fetch",
})
_NEW_LOW = frozenset({"knowledge", "defi_data"})


def test_new_money_or_exec_ids_are_high_risk():
    from core.tool_capabilities import high_risk_tool_ids
    assert _NEW_HIGH <= high_risk_tool_ids()


def test_new_high_impact_only_ids_are_medium_risk():
    from core.tool_capabilities import high_risk_tool_ids, medium_risk_tool_ids
    assert _NEW_MEDIUM.isdisjoint(high_risk_tool_ids())
    assert _NEW_MEDIUM <= medium_risk_tool_ids()


def test_new_bare_capability_ids_are_low_risk():
    from core.tool_capabilities import high_risk_tool_ids, medium_risk_tool_ids
    assert _NEW_LOW.isdisjoint(high_risk_tool_ids())
    assert _NEW_LOW.isdisjoint(medium_risk_tool_ids())


def test_new_ids_capability_sets_match_the_tier_rule():
    """Cross-check the OTHER direction: every id I claim is _NEW_HIGH really does
    carry money/exec, every _NEW_MEDIUM id really is high_impact-only, every
    _NEW_LOW id really has an empty capability set."""
    for t in _NEW_HIGH:
        caps = TOOL_CAPABILITIES[t]
        assert "money" in caps or "exec" in caps, f"{t}: {caps}"
    for t in _NEW_MEDIUM:
        caps = TOOL_CAPABILITIES[t]
        assert "high_impact" in caps and "money" not in caps and "exec" not in caps, f"{t}: {caps}"
    for t in _NEW_LOW:
        assert TOOL_CAPABILITIES[t] == frozenset(), f"{t}: {TOOL_CAPABILITIES[t]}"
