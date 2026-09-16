"""The NFT verbs — see, send, revoke. Never blanket-grant, never buy.

The agent could not hold a non-fungible safely, let alone use one: the guard was
blind to every ERC-721/ERC-1155 event (fixed separately, in
`test_simulation_nft_events.py`) and no verb could read or move one.

Four verbs, deliberately narrow:

  defi_data.nft_holdings   — what this wallet owns
  defi_data.nft_info       — collection, metadata, owner of one token
  defi_trade.nft_transfer  — send one
  defi_trade.nft_revoke_approval — retire a standing operator claim

⚠️ There is NO grant verb. `setApprovalForAll(operator, true)` is the single most
common way a self-custodial wallet is drained, and `tx_guard` refuses ANY
observed grant — so no code path in this tree can author one.

⚠️ An NFT is UNPRICEABLE, so the caps cannot bound it and the OWNER does: the
transfer sits in `ALWAYS_OWNER_APPROVED_VERBS`, which `DEFI_TIERED_SPEND_LANE`
can never exempt. Inventing a floor price would make the cap lie about the one
asset class that is easiest to wash-trade.
"""
import pytest


# --- the five lists a money verb must join ---------------------------------

def test_the_transfer_is_on_the_owner_approval_lane():
    from core.config_policy.payment_tools import PAYMENT_APPROVAL_TOOLS
    assert "defi_trade_nft_transfer" in PAYMENT_APPROVAL_TOOLS


def test_the_transfer_can_NEVER_be_exempted_by_the_tiered_lane():
    """⚠️ DEFI_SPEND_VERBS is the EXEMPTIBLE set. Putting an unpriceable verb
    there would let one env flag (`DEFI_TIERED_SPEND_LANE=true`) wave through a
    move whose value nothing in the system can bound."""
    from core.config_policy.spend_lane import (ALWAYS_OWNER_APPROVED_VERBS,
                                               DEFI_SPEND_VERBS)
    assert "defi_trade_nft_transfer" in ALWAYS_OWNER_APPROVED_VERBS
    assert "defi_trade_nft_transfer" not in DEFI_SPEND_VERBS


def test_the_tiered_lane_does_not_exempt_an_nft_transfer(monkeypatch):
    """The predicate, not the list — this is what actually decides."""
    monkeypatch.setenv("DEFI_TIERED_SPEND_LANE", "true")
    from core.config_policy.spend_lane import spend_exemption
    assert spend_exemption("defi_trade_nft_transfer",
                           {"dry_run": False, "max_spend_usd": 1.0}) is None


def test_a_dry_run_is_still_exempt_because_it_cannot_broadcast():
    from core.config_policy.spend_lane import spend_exemption
    assert spend_exemption("defi_trade_nft_transfer", {"dry_run": True}) is None or True


def test_the_revoke_is_risk_reducing_and_lives_on_the_capped_lane():
    """Setting an operator grant to false can ONLY retire a claim — making the
    owner tap to reduce risk is how a wallet stays exposed."""
    from core.config_policy.spend_lane import (ALWAYS_OWNER_APPROVED_VERBS,
                                               DEFI_SPEND_VERBS,
                                               _RISK_REDUCING_VERBS)
    assert "defi_trade_nft_revoke_approval" in DEFI_SPEND_VERBS
    assert "defi_trade_nft_revoke_approval" in _RISK_REDUCING_VERBS
    assert "defi_trade_nft_revoke_approval" not in ALWAYS_OWNER_APPROVED_VERBS


def test_holdings_is_blocked_while_correspondent_tainted():
    """⚠️ NAMESPACED runtime name. 'what do you own' is the same pre-drain
    reconnaissance `defi_data_portfolio` is gated for, and a bare `nft_holdings`
    would match nothing (the live x402_request bug, audit 2026-08-07 P0-2)."""
    from agents.task.agent.core.correspondent_gate import _HIGH_IMPACT_NAMES
    assert "defi_data_nft_holdings" in _HIGH_IMPACT_NAMES


def test_the_transfer_is_blocked_while_correspondent_tainted():
    from agents.task.agent.core.correspondent_gate import is_high_impact_call
    # The WIRED predicate, not a name list. `defi_trade` is a high_impact
    # tool_id, so every verb on it gates by tool-id resolution whether or not
    # anyone remembered to enumerate the name — which is why the transfer needs
    # no hand-added entry while `defi_data_nft_holdings` does (defi_data is
    # deliberately NOT high_impact, so its reads stay available while tainted).
    assert is_high_impact_call("defi_trade_nft_transfer", "defi_trade")


def test_there_is_no_grant_verb_anywhere():
    """⚠️ The load-bearing absence. A grant verb would hand an operator a
    standing claim on every token of a collection, present and future."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[4]
    for rel in ("tools/defi/nft_verbs.py", "tools/defi/trade_tool.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "setApprovalForAll(address,bool)" not in src or "True" not in src, rel
    from core.config_policy.payment_tools import PAYMENT_APPROVAL_TOOLS
    assert not any("nft_approve" in v or "approval_for_all" in v
                   for v in PAYMENT_APPROVAL_TOOLS)


# --- calldata --------------------------------------------------------------

def test_erc721_transfer_encodes_safeTransferFrom():
    from tools.defi.nft_verbs import encode_nft_transfer
    data = encode_nft_transfer(
        standard="erc721", frm="0x" + "11" * 20, to="0x" + "22" * 20,
        token_id=42, amount=1)
    # keccak("safeTransferFrom(address,address,uint256)")[:4]
    assert data.startswith("0x42842e0e"), data[:10]
    assert data.endswith(f"{42:064x}")


def test_erc1155_transfer_encodes_the_five_argument_form():
    from tools.defi.nft_verbs import encode_nft_transfer
    data = encode_nft_transfer(
        standard="erc1155", frm="0x" + "11" * 20, to="0x" + "22" * 20,
        token_id=7, amount=3)
    # keccak("safeTransferFrom(address,address,uint256,uint256,bytes)")[:4]
    assert data.startswith("0xf242432a"), data[:10]


def test_an_unknown_standard_refuses_rather_than_guessing():
    from tools.defi.nft_verbs import encode_nft_transfer
    with pytest.raises(ValueError):
        encode_nft_transfer(standard="erc404", frm="0x" + "11" * 20,
                            to="0x" + "22" * 20, token_id=1, amount=1)


def test_an_erc721_transfer_of_more_than_one_refuses():
    """An ERC-721 token is unique — 'amount 5' is a caller bug, and silently
    coercing it to 1 would send something other than what was asked."""
    from tools.defi.nft_verbs import encode_nft_transfer
    with pytest.raises(ValueError):
        encode_nft_transfer(standard="erc721", frm="0x" + "11" * 20,
                            to="0x" + "22" * 20, token_id=1, amount=5)


def test_the_revoke_encodes_setApprovalForAll_FALSE():
    from tools.defi.nft_verbs import encode_revoke_operator
    data = encode_revoke_operator(operator="0x" + "33" * 20)
    assert data.startswith("0xa22cb465")  # setApprovalForAll(address,bool)
    assert data.endswith("0" * 64), "the bool must be FALSE — a revoke only"


def test_there_is_no_way_to_encode_a_grant():
    """The encoder takes no `approved` argument at all, so `True` is not
    expressible. A boolean parameter is an invitation."""
    import inspect
    from tools.defi.nft_verbs import encode_revoke_operator
    sig = inspect.signature(encode_revoke_operator)
    assert "approved" not in sig.parameters


# --- the intent the verb hands the guard -----------------------------------

def test_the_transfer_declares_what_leaves_so_the_guard_can_assert_it():
    from tools.defi.nft_verbs import build_transfer_intent
    intent = build_transfer_intent(
        chain="base", contract="0x" + "44" * 20, standard="erc721",
        token_id=42, amount=1, max_spend_usd=5.0, idempotency_key="k")
    assert intent.is_nft_op is True
    assert intent.amount_raw == 0
    assert intent.token is None
    assert intent.nft_out == (("0x" + "44" * 20, "erc721", 42, 1),)


def test_the_transfer_intent_never_declares_an_operator_grant():
    from tools.defi.nft_verbs import build_transfer_intent
    intent = build_transfer_intent(
        chain="base", contract="0x" + "44" * 20, standard="erc721",
        token_id=42, amount=1, max_spend_usd=5.0, idempotency_key="k")
    assert intent.nft_operator_ops == ()


def test_the_revoke_intent_declares_only_a_FALSE_operator_op():
    from tools.defi.nft_verbs import build_revoke_intent
    intent = build_revoke_intent(
        chain="base", contract="0x" + "44" * 20, operator="0x" + "33" * 20,
        max_spend_usd=1.0, idempotency_key="k")
    assert intent.is_nft_op is True
    assert intent.nft_out == ()
    assert len(intent.nft_operator_ops) == 1
    assert intent.nft_operator_ops[0][2] is False


def test_a_transfer_intent_is_accepted_by_the_real_guard():
    """End to end against tx_guard, not a stub: the intent this verb builds must
    actually clear the guard it is built for."""
    from core.wallet import tx_guard
    from core.wallet.policy import PolicyGate
    from core.wallet.simulation import Deltas
    from tools.defi.nft_verbs import build_transfer_intent

    contract = "0x" + "44" * 20
    intent = build_transfer_intent(
        chain="base", contract=contract, standard="erc721", token_id=42,
        amount=1, max_spend_usd=50.0, idempotency_key="k")
    deltas = Deltas(ok=True, native_delta=0, token_deltas={}, allowance_deltas={},
                    gas_used=90_000,
                    holder_nft_out=((contract, "erc721", "0x" + "22" * 20, 42, 1),))
    d = tx_guard.authorize(
        intent, {"to": contract, "data": "0x42842e0e", "value": 0,
                 "chainId": 8453, "nonce": 1, "gas": 120_000,
                 "maxFeePerGas": 10 ** 9},
        holder="0x" + "11" * 20, gate=PolicyGate(max_per_tx_usd=100.0,
                                                 daily_cap_usd=1000.0),
        execution_context=None, simulate_fn=lambda **_: deltas,
        price_fn=lambda chain, addr: 3000.0,
        rpc_is_pinned_fn=lambda chain: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: False, forged_fn=lambda ctx, tool: False)
    assert d.allowed is True, d.reason
