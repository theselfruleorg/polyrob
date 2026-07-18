"""Cross-consistency invariants for the hand-maintained dangerous-tool sets
(audit T12, 2026-07-16).

Six separate frozensets gate dangerous capability (DELEGATE_BLOCKED_TOOLS,
HIGH_IMPACT_TOOL_IDS/_HIGH_IMPACT_NAMES/_HIGH_IMPACT_VERB_SUBSTRINGS,
MONEY_TOOLS, the approval unions) and all must co-evolve when a tool is added —
a new money/exec tool added to one but not the others leaks silently. These
invariants are the cheap interim version of a per-tool capability registry:
they fail the build instead.

Polarity differences between the sets are INTENTIONAL (e.g. crypto tool_ids are
deliberately absent from HIGH_IMPACT_TOOL_IDS so read verbs stay allowed while
tainted; the trade verbs are gated by name/substring instead) — the tests below
encode the designed relationships, not naive equality.
"""

# Not a registrable tool_id anywhere yet — enumerated in the gate sets
# defensively ("dynamic-tool authoring", P1 aspirational). Remove from this
# allowlist the day a `tool_manage` tool registers, so parity bites.
_ASPIRATIONAL_IDS = {"tool_manage"}


def test_money_tools_are_delegate_blocked():
    """A leaf/delegated agent must never wield a money tool."""
    from agents.task.runtime.metering_gate import MONEY_TOOLS
    from tools.controller.delegation import DELEGATE_BLOCKED_TOOLS

    leak = set(MONEY_TOOLS) - set(DELEGATE_BLOCKED_TOOLS)
    assert leak == set(), f"money tools delegable to leaf agents: {sorted(leak)}"


def test_money_tools_covered_by_correspondent_gate():
    """Every money tool must be unusable from a correspondent-tainted session:
    x402_* via HIGH_IMPACT_TOOL_IDS (their every verb is money-shaped); the
    crypto venues via their trade VERBS (tool_ids deliberately stay readable)."""
    from agents.task.agent.core.correspondent_gate import (
        HIGH_IMPACT_TOOL_IDS, is_high_impact)

    assert "x402_pay" in HIGH_IMPACT_TOOL_IDS
    assert "x402_invoice" in HIGH_IMPACT_TOOL_IDS

    # H10 class: BOTH the bare and the venue-namespaced runtime trade verbs gate…
    for venue in ("hyperliquid", "polymarket"):
        for verb in ("place_limit_order", "place_market_order", "cancel_order"):
            assert is_high_impact(verb), verb
            assert is_high_impact(f"{venue}_{verb}"), f"{venue}_{verb}"
        # …while reads stay allowed (the reason the tool_ids aren't blocked).
        assert not is_high_impact(f"{venue}_get_trade_history"), venue


def test_gate_sets_reference_real_tool_ids():
    """Every tool_id a gate names must exist in the tool vocabulary — a typo'd
    or renamed id in a security set silently gates nothing.

    Since 2026-07-16 VALID_TOOL_IDS derives from the same capability table the
    gate sets derive from, so comparing against it became tautological. Compare
    against the INDEPENDENT registry-side vocabulary instead: descriptor display
    names (post register_optional_tool side effects) + the enumerated flag-gated
    optional ids (same expectation as test_valid_tool_ids_parity)."""
    import tools  # noqa: F401 — triggers register_optional_tool side effects
    from tools.descriptors import TOOL_DESCRIPTORS, get_tool_display_name
    from agents.task.agent.core.correspondent_gate import HIGH_IMPACT_TOOL_IDS
    from agents.task.runtime.metering_gate import MONEY_TOOLS
    from tools.controller.delegation import DELEGATE_BLOCKED_TOOLS

    registry_vocab = {get_tool_display_name(n) for n in TOOL_DESCRIPTORS} | {
        # Flag-gated optional tools not registered under the default test env.
        "shell", "process", "self_env", "hf_deploy", "github", "coding",
        "code_execution", "git", "goal", "cronjob", "knowledge",
        "x402_pay", "x402_invoice",
    }
    named = set(DELEGATE_BLOCKED_TOOLS) | set(MONEY_TOOLS) | set(HIGH_IMPACT_TOOL_IDS)
    unknown = named - registry_vocab - _ASPIRATIONAL_IDS
    assert unknown == set(), f"gate sets name unknown tool ids: {sorted(unknown)}"


def test_correspondent_trade_verbs_in_sync():
    """correspondent_gate.py carries an explicit 'keep in sync' comment between
    _HIGH_IMPACT_VERB_SUBSTRINGS and the trade verbs in _HIGH_IMPACT_NAMES —
    enforce it: every substring must anchor to an enumerated name."""
    from agents.task.agent.core import correspondent_gate as cg

    orphans = [s for s in cg._HIGH_IMPACT_VERB_SUBSTRINGS
               if not any(s in name for name in cg._HIGH_IMPACT_NAMES)]
    assert orphans == [], f"verb substrings with no _HIGH_IMPACT_NAMES anchor: {orphans}"
