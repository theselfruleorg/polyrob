"""Every money-spend verb must be in EVERY gate. Bidirectional.

The existing parity ratchet is ONE-directional: it catches a name that no longer
exists, and says nothing about a name that was never added. `x402_fetch` was the
living proof — a money verb with no forged-turn refusal and no place on the
approval lane, sitting there unnoticed. Adding `defi_trade_solana_swap` would
have slipped through the same hole.

So this asserts the other direction: for every verb the capability table calls a
money-SPEND verb, it is on the approval lane, in the tiered spend lane, and in
the correspondent-taint high-impact set. Missing a gate is the failure mode that
costs money; a stale name merely costs tidiness.
"""
import pytest


def _spend_verbs():
    """Action names on a tool the capability table marks `money`, excluding the
    receivables-only tool (creating an invoice is not a spend)."""
    from core.tool_capabilities import ids_with
    money_tools = set(ids_with("money")) - {"x402_invoice"}
    from core.config_policy.payment_tools import PAYMENT_APPROVAL_TOOLS
    return {v for v in PAYMENT_APPROVAL_TOOLS
            if any(v.startswith(t + "_") or v == t for t in money_tools)}


def test_there_are_spend_verbs_to_check():
    assert _spend_verbs(), "the discovery above found nothing — the test is inert"


def test_every_spend_verb_is_classified_for_the_tiered_spend_lane():
    """Every defi spend verb is either EXEMPTIBLE by the tiered lane or
    explicitly NEVER exemptible. A verb in neither set was forgotten, and a
    forgotten verb queues or executes on the wrong lane.

    037 added the second bucket: `bridge` must never become exemptible, because
    the owner's decision is that it is always owner-approved. Silently adding it
    to DEFI_SPEND_VERBS to make this test pass would let one env flag overturn
    that."""
    from core.config_policy.spend_lane import (ALWAYS_OWNER_APPROVED_VERBS,
                                               DEFI_SPEND_VERBS)
    classified = set(DEFI_SPEND_VERBS) | set(ALWAYS_OWNER_APPROVED_VERBS)
    missing = {v for v in _spend_verbs() if v.startswith('defi_trade_')} - classified
    assert not missing, (
        f"money verbs classified for NEITHER lane: {sorted(missing)}. Add them to "
        f"DEFI_SPEND_VERBS (exemptible below the autonomous ceiling) or to "
        f"ALWAYS_OWNER_APPROVED_VERBS (never exemptible), with the reason.")


def test_the_two_lane_buckets_do_not_overlap():
    """A verb cannot be both exemptible and never-exemptible."""
    from core.config_policy.spend_lane import (ALWAYS_OWNER_APPROVED_VERBS,
                                               DEFI_SPEND_VERBS)
    assert not (set(DEFI_SPEND_VERBS) & set(ALWAYS_OWNER_APPROVED_VERBS))


def test_the_bridge_is_on_the_capped_lane():
    """SUPERSEDES the 2026-09-11 always-approve pin. The owner used that shape
    and rejected it on 2026-09-12: a money rail needing a tap per move is not
    autonomy. Caps, not taps — the bridge is bounded by the per-tx ceiling, the
    rolling daily cap and its simulated + asserted deltas, with the owner queue
    still catching anything over DEFI_AUTONOMOUS_MAX_USD."""
    from core.config_policy.spend_lane import (ALWAYS_OWNER_APPROVED_VERBS,
                                               DEFI_SPEND_VERBS)
    assert "defi_trade_bridge" in DEFI_SPEND_VERBS
    assert "defi_trade_bridge" not in ALWAYS_OWNER_APPROVED_VERBS


def test_every_spend_verb_is_blocked_while_correspondent_tainted():
    """Asserted against the PREDICATE, not the name set.

    The gate has three paths — an explicit name, a prefix, and a verb substring
    — and a verb caught by the substring rule is just as blocked as one listed
    by name. Checking only the name set reports false gaps (it flagged the
    hyperliquid/polymarket order verbs, which `is_high_impact` catches on
    "order") and would also miss a verb that is in the set but which the
    predicate somehow does not honour. The predicate is what runs.
    """
    from agents.task.agent.core.correspondent_gate import is_high_impact
    missing = {v for v in _spend_verbs() if not is_high_impact(v)}
    assert not missing, (
        f"money verbs reachable from a correspondent-tainted session: "
        f"{sorted(missing)}. An injected third party could reach them.")


def test_the_solana_swap_verb_is_registered_everywhere():
    """The verb this test was written alongside — pinned by name so a future
    refactor cannot quietly drop it."""
    from core.config_policy.payment_tools import PAYMENT_APPROVAL_TOOLS
    from core.config_policy.spend_lane import DEFI_SPEND_VERBS
    from agents.task.agent.core.correspondent_gate import (_HIGH_IMPACT_NAMES,
                                                           is_high_impact)
    name = "defi_trade_solana_swap"
    assert name in PAYMENT_APPROVAL_TOOLS
    assert name in DEFI_SPEND_VERBS
    assert name in _HIGH_IMPACT_NAMES        # listed explicitly, not just matched
    assert is_high_impact(name)              # and the predicate honours it
