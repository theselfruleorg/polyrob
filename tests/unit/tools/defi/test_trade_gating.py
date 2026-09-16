"""defi_trade gating — every hand-maintained list, asserted by RUNTIME name.

A money verb is only as gated as its weakest list, and each of these lists is
hand-maintained. The x402_request lane was dead for months because the list held
a bare name; these assertions use the namespaced runtime name deliberately.

T4 review (2026-08-14): when swap/approve_token/revoke_approval landed, NONE of
them was added to any list — they bypassed the owner-approval lane entirely and
the correspondent gate's name layer (only tool-id resolution stood between a
tainted session and a swap). Every assertion here now runs over ALL FOUR money
verbs so the next verb cannot ship unlisted.
"""
import pytest

from agents.task.agent.core.correspondent_gate import is_high_impact
from core.config_policy import PAYMENT_APPROVAL_TOOLS, PAYMENT_RECEIVE_APPROVAL_TOOLS
from core.tool_capabilities import TOOL_CAPABILITIES, ids_with, is_classified

TRANSFER = "defi_trade_transfer"

#: EVERY money verb the tool registers, by namespaced runtime name. Grows when
#: a verb is added to DefiTradeTool — test_every_money_verb_is_listed_here
#: fails if the tool and this list drift.
MONEY_VERBS = (
    "defi_trade_transfer",
    "defi_trade_swap",
    "defi_trade_approve_token",
    "defi_trade_revoke_approval",
    # Solana Phase 3. One verb, not three: Jupiter needs no standing delegate,
    # so approve/revoke have no meaning on that chain.
    "defi_trade_solana_swap",
    # 037: the cross-chain bridge. Unlike every verb above it has NO autonomous
    # lane and NO dollar cap — the owner's approval is the cap — so these four
    # lists are a floor under that policy, not the policy itself.
    "defi_trade_bridge",
    # 2026-09-13: wrapping native into its ERC-20 form. It moves no value out of
    # the wallet -- 1:1, same wallet, pinned destination -- but it IS a signed
    # native-value send, and "not really a spend" is the reasoning that left
    # solana_swap and x402_fetch ungoverned.
    "defi_trade_wrap",
    # 2026-09-14: the inverse. `wrap` shipped one-way, which left a wallet that
    # wrapped its gas to trade unable to pay for a transaction. Same reasoning as
    # wrap for why it is governed: it moves no value out, but it is a signed
    # transaction against a money contract from the treasury wallet.
    "defi_trade_unwrap",
    # 2026-09-15: the non-fungible verbs. `nft_transfer` is the FIRST verb with
    # no cap that means anything -- a collectible has no reliable price, so
    # tx_guard prices only the FEE and the owner is the bound
    # (ALWAYS_OWNER_APPROVED_VERBS). `nft_revoke_approval` grants nothing and
    # moves nothing; it only retires a standing operator claim, so it is on the
    # capped lane as risk-reducing. There is deliberately NO grant verb.
    "defi_trade_nft_transfer",
    "defi_trade_nft_revoke_approval",
    # 046: the agent's own ERC-8004 identity. Both send nothing -- their cost
    # is the fee -- but both are signed transactions from the treasury wallet
    # that create or rewrite a PERMANENT public identity.
    "defi_trade_register_agent",
    "defi_trade_set_agent_uri",
    # 042: deployment (the pinned token template and caller-supplied bytecode)
    # and the generic contract call.
    "defi_trade_deploy_token",
    "defi_trade_deploy_contract",
    "defi_trade_call",
    "defi_trade_lp_add", "defi_trade_lp_remove", "defi_trade_lp_collect",
    "defi_trade_solana_deploy_token",
)


def test_every_money_verb_is_listed_here():
    """Derive the tool's action set so a NEW verb cannot ship without joining
    MONEY_VERBS (and so every list below)."""
    import inspect
    from tools.defi.trade_tool import DefiTradeTool

    derived = set()
    for attr in dir(DefiTradeTool):
        if attr.startswith("_"):
            continue
        member = inspect.getattr_static(DefiTradeTool, attr)
        if callable(member) and (hasattr(member, "action_info")
                                 or hasattr(member, "_description")):
            derived.add(f"defi_trade_{attr}")
    assert derived == set(MONEY_VERBS), (
        f"DefiTradeTool's verbs {sorted(derived)} drifted from MONEY_VERBS — "
        "every new money verb must join the approval lane and the taint gate"
    )


def test_defi_trade_is_classified_as_money():
    assert is_classified("defi_trade")
    caps = TOOL_CAPABILITIES["defi_trade"]
    assert "money" in caps
    assert "high_impact" in caps
    assert "delegate_blocked" in caps


def test_a_delegated_leaf_cannot_reach_it():
    from tools.controller.delegation import get_blocked_child_tools
    assert "defi_trade" in get_blocked_child_tools()
    assert "defi_trade" in ids_with("delegate_blocked")


@pytest.mark.parametrize("verb", MONEY_VERBS)
def test_every_money_verb_is_on_an_owner_approval_lane(verb):
    """Two legal homes, and a verb must be in exactly one (039).

    Most verbs ride the shared pre-hook. A verb may instead own its gate inside
    itself — but only by declaring it in `VERB_OWNED_APPROVAL_GATES`, so the
    exception is a registry entry someone has to write, never a verb quietly
    absent from both lists.

    `defi_trade_bridge` earned that: it knows the recipient, the USD value, the
    arrival floor and the Relay request id, none of which the raw action params
    carry. Carrying BOTH gates meant two taps for one bridge from two different
    prompts, which is what the owner hit on 2026-09-12.
    """
    from core.config_policy.payment_tools import VERB_OWNED_APPROVAL_GATES
    in_shared = verb in PAYMENT_APPROVAL_TOOLS
    in_own = verb in VERB_OWNED_APPROVAL_GATES
    assert in_shared or in_own, (
        f"{verb} is on NO owner-approval lane. Add it to PAYMENT_APPROVAL_TOOLS, "
        f"or declare its in-verb gate in VERB_OWNED_APPROVAL_GATES.")
    assert not (in_shared and in_own), (
        f"{verb} is on BOTH lanes — that is two owner taps for one action.")


def test_a_verb_owned_gate_names_where_it_lives():
    """A registry entry that does not point at real code is a hole with a
    comment on it."""
    import os
    from core.config_policy.payment_tools import VERB_OWNED_APPROVAL_GATES
    for verb, where in VERB_OWNED_APPROVAL_GATES.items():
        path, _, symbol = where.partition("::")
        assert os.path.isfile(path), f"{verb}: {path} does not exist"
        assert symbol and symbol in open(path).read(), f"{verb}: {where} not found"


@pytest.mark.parametrize("verb", MONEY_VERBS)
def test_every_money_verb_is_on_the_SPEND_lane_not_receive(verb):
    """Receive-side verbs may act-and-report under PAYMENT_APPROVAL_MODE=auto.
    Moving funds out (or granting a standing claim on them) must never be
    act-and-report."""
    assert verb not in PAYMENT_RECEIVE_APPROVAL_TOOLS


@pytest.mark.parametrize("verb", MONEY_VERBS)
def test_every_money_verb_is_high_impact_for_a_tainted_session(verb):
    """The NAME layer alone must block it — this is what survives a tool-id
    resolver fault, the single point of failure the name entries remove."""
    assert is_high_impact(verb) is True


def test_bare_name_is_not_what_the_lists_rely_on():
    """Guards the regression directly: if someone 'simplifies' the entry to the
    bare verb, the runtime name stops matching and every gate silently dies."""
    assert "transfer" not in PAYMENT_APPROVAL_TOOLS


def test_flag_defaults_off(monkeypatch):
    from tools.defi import defi_trade_enabled
    monkeypatch.delenv("DEFI_TRADE_ENABLED", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    assert defi_trade_enabled() is False


def test_flag_never_rides_the_local_safe_group(monkeypatch):
    from tools.defi import defi_trade_enabled
    monkeypatch.delenv("DEFI_TRADE_ENABLED", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    assert defi_trade_enabled() is False


def test_never_in_the_default_toolset_even_when_enabled(monkeypatch):
    """Money tools are explicit-grant-only — never auto-loaded into a session."""
    monkeypatch.setenv("DEFI_TRADE_ENABLED", "true")
    monkeypatch.delenv("POLYROB_AGENT_TOOLSET", raising=False)
    from agents.task.tool_defaults import _dynamic_default_tools
    assert "defi_trade" not in _dynamic_default_tools()


def test_load_tool_refuses_to_self_serve_a_money_tool():
    from tools.tool_disclosure import perform_load_tool  # noqa: F401
    from core.tool_capabilities import TOOL_CAPABILITIES
    assert "money" in TOOL_CAPABILITIES["defi_trade"], (
        "the load_tool money refusal is derived from this row")
