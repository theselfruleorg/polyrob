"""Characterization golden for the money authorizer (W6, 2026-10-10).

``tx_guard._authorize`` is split into ordered step functions. This file pins
the EXACT :class:`~core.wallet.tx_guard.Decision` — every field, the reason
text included — that the public entry point returns over a broad matrix of
inputs, one or more per numbered step and per allow/deny branch. A refactor
that changes any outcome, any reason word, or which step answers first fails
here before it can reach a wallet.

The expected values live in ``tx_guard_golden.json`` next to this file. To
regenerate them after an INTENDED behaviour change (and review the diff)::

    python -m tests.unit.core.wallet.test_tx_guard_golden --regen
"""
from __future__ import annotations

import json
import sys
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.wallet import tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

GOLDEN = Path(__file__).with_name("tx_guard_golden.json")
#: Set only by the manual ``--regen`` tool below.
_REGEN_ROWS = None

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
TO = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
SPENDER = "0x1111111111111111111111111111111111111111"
HOLDER = "0x2222222222222222222222222222222222222222"
OTHER = "0x3333333333333333333333333333333333333333"
TOKEN_X = "0x4444444444444444444444444444444444444444"
NFT = "0x5555555555555555555555555555555555555555"
ACCOUNT = "0x6666666666666666666666666666666666666666"
ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
LIFI = "0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE"
REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
FACTORY = "0x4e59b44847b379578588920cA78FbF26c0B4956C"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
ZERO = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dEaD"
OWNER = "owner-1"
ETH = 10 ** 18


def _transfer_data(to: str, amount: int) -> str:
    return "0xa9059cbb" + "0" * 24 + to.lower()[2:] + format(amount, "064x")


def _approve_data(spender: str, amount: int) -> str:
    return "0x095ea7b3" + "0" * 24 + spender.lower()[2:] + format(amount, "064x")


# --------------------------------------------------------------------------
# Scenario building blocks
# --------------------------------------------------------------------------

BASE_INTENT = dict(chain="base", token=USDC, to=TO, amount_raw=250_000,
                   max_spend_usd=0.30, idempotency_key="k1")
BASE_TX = {"to": USDC, "data": "0xa9059cbb", "value": 0, "chainId": 8453}
BASE_DELTAS = dict(ok=True, native_delta=0, token_deltas={USDC: -250_000},
                   allowance_deltas={(USDC, SPENDER): 0})

NATIVE_INTENT = dict(chain="base", token=None, to=TO, amount_raw=ETH // 100,
                     max_spend_usd=100.0, idempotency_key="kn")
NATIVE_TX = {"to": TO, "data": "0x", "value": ETH // 100, "chainId": 8453}
NATIVE_DELTAS = dict(ok=True, native_delta=-(ETH // 100), token_deltas={})

APPROVE_INTENT = dict(chain="base", token=USDC, to=ROUTER, amount_raw=0,
                      max_spend_usd=5.0, is_allowance_op=True,
                      expected_allowance_grants=((USDC, ROUTER, 1_000_000),),
                      idempotency_key="ka")
APPROVE_TX = {"to": USDC, "data": _approve_data(ROUTER, 1_000_000), "value": 0,
              "chainId": 8453}
APPROVE_DELTAS = dict(ok=True, native_delta=0, token_deltas={USDC: 0},
                      allowance_deltas={(USDC, ROUTER): 1_000_000})

REVOKE_INTENT = dict(chain="base", token=USDC, to=SPENDER, amount_raw=0,
                     max_spend_usd=2.0, is_allowance_op=True,
                     idempotency_key="kr")
REVOKE_TX = {"to": USDC, "data": _approve_data(SPENDER, 0), "value": 0, "chainId": 8453}
REVOKE_DELTAS = dict(ok=True, native_delta=0, token_deltas={USDC: 0},
                     allowance_deltas={(USDC, SPENDER): 0})

CLAIM_INTENT = dict(chain="base", token=None, to=OTHER, amount_raw=0,
                    max_spend_usd=5.0, is_claim=True,
                    min_native_inflow_wei=ETH // 1000, idempotency_key="kc")
CLAIM_TX = {"to": OTHER, "data": "0x4e71d92d", "value": 0, "chainId": 8453,
            "gas": 100_000, "maxFeePerGas": 1_000_000}
CLAIM_DELTAS = dict(ok=True, native_delta=ETH // 100, token_deltas={}, gas_used=60_000)

NFT_INTENT = dict(chain="base", token=None, to=NFT, amount_raw=0, max_spend_usd=5.0,
                  is_nft_op=True, nft_out=((NFT, "erc721", 7, 1),), idempotency_key="kf")
NFT_TX = {"to": NFT, "data": "0x42842e0e", "value": 0, "chainId": 8453,
          "gas": 100_000, "maxFeePerGas": 1_000_000}
NFT_DELTAS = dict(ok=True, native_delta=0, token_deltas={}, gas_used=50_000,
                  holder_nft_out=((NFT, "erc721", OTHER, 7, 1),))

REG_INTENT = dict(chain="base", token=None, to=REGISTRY, amount_raw=0, max_spend_usd=5.0,
                  is_registration=True, expected_registry=REGISTRY, idempotency_key="kg")
REG_TX = {"to": REGISTRY, "data": "0xf2c298be", "value": 0, "chainId": 8453,
          "gas": 200_000, "maxFeePerGas": 1_000_000}
REG_DELTAS = dict(ok=True, native_delta=0, token_deltas={}, gas_used=150_000,
                  holder_nft_in=((REGISTRY, "erc721", ZERO, 42, 1),))

SWAP_INTENT = dict(chain="base", token=TOKEN_X, to=ROUTER, amount_raw=1_000,
                   max_spend_usd=5.0, inflow_token=USDC, min_inflow_raw=900,
                   held_balance_raw=5_000, idempotency_key="ks")
SWAP_TX = {"to": ROUTER, "data": "0x5ae401dc", "value": 0, "chainId": 8453}
SWAP_DELTAS = dict(ok=True, native_delta=0, token_deltas={TOKEN_X: -1_000, USDC: 950})


def _owner_ctx(**kw):
    base = dict(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                session_id="s-owner", metadata={})
    base.update(kw)
    return SimpleNamespace(**base)


def _raise(exc):
    def boom(*_a, **_k):
        raise exc
    return boom


def S(base_intent=BASE_INTENT, base_tx=BASE_TX, base_deltas=BASE_DELTAS, *,
      intent=None, tx=None, tx_replace=None, deltas=None, deltas_replace=None,
      ctx=None, forged=False, forged_fn="default", autonomous_ok=None,
      halted=False, entry_paused=False, pinned=True, price=1.0, prices=None,
      fallback=None, gate=None, simulate=None, env=None, liquidity_rpc=None,
      account_rpc=None, patches=None):
    """One scenario — a description, evaluated lazily by :func:`_run`."""
    return dict(base_intent=base_intent, base_tx=base_tx, base_deltas=base_deltas,
                intent=intent or {}, tx=tx or {}, tx_replace=tx_replace,
                deltas=deltas or {}, deltas_replace=deltas_replace, ctx=ctx,
                forged=forged, forged_fn=forged_fn, autonomous_ok=autonomous_ok,
                halted=halted, entry_paused=entry_paused, pinned=pinned, price=price,
                prices=prices, fallback=fallback, gate=gate or {}, simulate=simulate,
                env=env or {}, liquidity_rpc=liquidity_rpc, account_rpc=account_rpc,
                patches=patches or {})


SCENARIOS = {
    # -- happy paths ---------------------------------------------------------
    "erc20_send_allowed": S(),
    "native_send_allowed": S(NATIVE_INTENT, NATIVE_TX, NATIVE_DELTAS, price=2000.0),
    "approve_allowed": S(APPROVE_INTENT, APPROVE_TX, APPROVE_DELTAS),
    "revoke_allowed": S(REVOKE_INTENT, REVOKE_TX, REVOKE_DELTAS),
    "claim_allowed": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS, price=2000.0),
    "nft_send_allowed": S(NFT_INTENT, NFT_TX, NFT_DELTAS, price=2000.0),
    "registration_allowed": S(REG_INTENT, REG_TX, REG_DELTAS, price=2000.0),
    "swap_allowed": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS),
    # -- 0/1. owner authority + pause ---------------------------------------
    "pause_refuses": S(halted=True),
    "pause_probe_raises": S(halted=_raise(RuntimeError("probe down"))),
    "pause_owner_direct_passes": S(ctx="owner", halted=True),
    "pause_forged_ctx_refuses": S(ctx="owner", forged=True, halted=True),
    # -- 1b. entry pause -----------------------------------------------------
    "entry_pause_refuses_entry": S(entry_paused=True),
    "entry_pause_exit_revoke_passes": S(REVOKE_INTENT, REVOKE_TX, REVOKE_DELTAS,
                                        entry_paused=True),
    "entry_pause_exit_sell_passes": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS, entry_paused=True),
    "entry_pause_owner_direct_passes": S(ctx="owner", entry_paused=True),
    "entry_pause_probe_raises": S(entry_paused=_raise(RuntimeError("ep down"))),
    "entry_pause_reveal_skipped": S(intent=dict(is_collection_reveal=True, reveal_ids=(1,)),
                                    entry_paused=True),
    # -- 2. turn origin ------------------------------------------------------
    "ctx_without_detector": S(ctx="owner", forged_fn=None),
    "forged_refused": S(ctx="owner", forged=True),
    "forged_detector_raises": S(ctx="owner", forged_fn=_raise(RuntimeError("no origin"))),
    "forged_autonomous_allowed_no_daily_cap": S(
        ctx="owner", forged=True, autonomous_ok=True,
        env={"DEFI_AUTONOMOUS_TURN_TRADING": "true"},
        gate=dict(daily_cap_usd=None)),
    "forged_autonomous_allowed_daily_cap": S(
        ctx="owner", forged=True, autonomous_ok=True,
        env={"DEFI_AUTONOMOUS_TURN_TRADING": "true"}),
    "forged_autonomous_detector_raises": S(
        ctx="owner", forged=True, autonomous_ok=_raise(RuntimeError("x")),
        env={"DEFI_AUTONOMOUS_TURN_TRADING": "true"}),
    "monitor_exit_sell_allowed": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS, ctx="owner", forged=True,
                                   env={"DEFI_MONITOR_EXITS": "true"}),
    "monitor_exit_sell_no_inflow": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS, ctx="owner",
                                     forged=True, env={"DEFI_MONITOR_EXITS": "true"},
                                     intent=dict(min_inflow_raw=None),
                                     deltas=dict(token_deltas={TOKEN_X: -1_000, USDC: 0})),
    "monitor_exit_entry_refused": S(ctx="owner", forged=True,
                                    env={"DEFI_MONITOR_EXITS": "true"}),
    "monitor_exit_sub_agent_refused": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS,
                                        ctx=dict(is_sub_agent=True), forged=True,
                                        env={"DEFI_MONITOR_EXITS": "true"}),
    "principal_not_owner": S(ctx=dict(user_id="stranger")),
    "owner_grant_clears_ceiling": S(
        NATIVE_INTENT, NATIVE_TX, NATIVE_DELTAS, price=2000.0, ctx=dict(
            metadata={"owner_grant": {"approved": True, "max_spend_usd": 500.0}}),
        forged=True, autonomous_ok=True, env={"DEFI_AUTONOMOUS_TURN_TRADING": "true"},
        intent=dict(amount_raw=ETH // 20, max_spend_usd=500.0),
        tx=dict(value=ETH // 20), deltas=dict(native_delta=-(ETH // 20))),
    # -- 2b. token-bound account --------------------------------------------
    "journal_without_account": S(intent=dict(is_journal_entry=True)),
    "journal_leg_without_account": S(intent=dict(via_account_journal=True)),
    "batch_without_account": S(intent=dict(via_account_batch=True)),
    "via_account_not_execute": S(intent=dict(via_account=ACCOUNT)),
    "via_account_journal_bad_leg": S(intent=dict(via_account=ACCOUNT,
                                                 via_account_journal=True)),
    "via_account_journal_entry_bad": S(intent=dict(via_account=ACCOUNT,
                                                   is_journal_entry=True)),
    "account_admin_call_refused": S(tx=dict(data="0x51945447" + "0" * 192)),
    # -- 3. structural -------------------------------------------------------
    "negative_amount": S(intent=dict(amount_raw=-1)),
    "zero_amount_send": S(intent=dict(amount_raw=0)),
    "liquidity_leaf_refused": S(intent=dict(is_liquidity_op=True), ctx=dict(role="leaf")),
    "liquidity_structural_refused": S(intent=dict(is_liquidity_op=True)),
    # -- 3a. LI.FI floor -----------------------------------------------------
    "lifi_floor_unknown_facet": S(SWAP_INTENT, dict(SWAP_TX, to=LIFI), SWAP_DELTAS,
                                  intent=dict(to=LIFI)),
    "lifi_floor_not_diamond": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS),
    # -- 3. shapes -----------------------------------------------------------
    "reveal_structural_refused": S(intent=dict(is_collection_reveal=True, reveal_ids=(1,))),
    "reveal_ids_without_flag": S(intent=dict(reveal_ids=(1,))),
    "mint_structural_refused": S(intent=dict(is_collection_mint=True, mint_ids=(1,))),
    "mint_ids_without_flag": S(intent=dict(mint_ids=(1,))),
    "registration_and_claim": S(REG_INTENT, REG_TX, REG_DELTAS, intent=dict(is_claim=True)),
    "registration_no_registry": S(REG_INTENT, REG_TX, REG_DELTAS,
                                  intent=dict(expected_registry=None)),
    "registration_unpinned_chain": S(REG_INTENT, REG_TX, REG_DELTAS,
                                     intent=dict(chain="robinhood")),
    "registration_wrong_registry": S(REG_INTENT, REG_TX, REG_DELTAS,
                                     intent=dict(expected_registry=OTHER)),
    "registration_intent_to_mismatch": S(REG_INTENT, REG_TX, REG_DELTAS,
                                         intent=dict(to=OTHER)),
    "registration_tx_to_mismatch": S(REG_INTENT, REG_TX, REG_DELTAS, tx=dict(to=OTHER)),
    "nft_and_claim": S(NFT_INTENT, NFT_TX, NFT_DELTAS, intent=dict(is_claim=True)),
    "nft_declares_nothing": S(NFT_INTENT, NFT_TX, NFT_DELTAS, intent=dict(nft_out=())),
    "nft_operator_grant_declared": S(NFT_INTENT, NFT_TX, NFT_DELTAS,
                                     intent=dict(nft_operator_ops=((NFT, OTHER, True),))),
    "erc6909_without_nft_flag": S(intent=dict(erc6909_revokes=((NFT, OTHER, 1),))),
    "nft_revoke_without_nft_flag": S(intent=dict(nft_approval_revokes=((NFT, 1),))),
    "permit2_revoke_not_allowance": S(intent=dict(permit2_revokes=((USDC, ROUTER),))),
    "permit2_revoke_wrong_to": S(REVOKE_INTENT, REVOKE_TX, REVOKE_DELTAS,
                                 intent=dict(permit2_revokes=((USDC, ROUTER),))),
    "permit2_revoke_token_missing": S(REVOKE_INTENT, dict(REVOKE_TX, to=PERMIT2),
                                      REVOKE_DELTAS,
                                      intent=dict(to=PERMIT2, token=TOKEN_X,
                                                  permit2_revokes=((USDC, ROUTER),))),
    "permit2_revoke_unmeasured": S(REVOKE_INTENT, dict(REVOKE_TX, to=PERMIT2), REVOKE_DELTAS,
                                   intent=dict(to=PERMIT2, permit2_revokes=((USDC, ROUTER),))),
    "permit2_revoke_measured": S(
        REVOKE_INTENT, dict(REVOKE_TX, to=PERMIT2), REVOKE_DELTAS,
        intent=dict(to=PERMIT2, permit2_revokes=((USDC, ROUTER),)),
        deltas=dict(holder_permit2_grants=((PERMIT2, USDC, ROUTER, 0),))),
    "revoke_wrong_shape": S(REVOKE_INTENT, dict(REVOKE_TX, data=_approve_data(OTHER, 0)),
                            REVOKE_DELTAS),
    "claim_and_deploy": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS, intent=dict(is_deploy=True)),
    "claim_and_allowance": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS,
                             intent=dict(is_allowance_op=True)),
    "claim_no_to": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS, intent=dict(to=None)),
    "claim_tx_to_mismatch": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS, tx=dict(to=TO)),
    "claim_with_token": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS, intent=dict(token=USDC)),
    "claim_with_amount": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS, intent=dict(amount_raw=5)),
    "claim_no_minimum": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS,
                          intent=dict(min_native_inflow_wei=None)),
    "create2_wrong_intent_to": S(intent=dict(is_deploy=True, create2_salt="0x" + "0" * 64,
                                             token=None, to=OTHER)),
    "create2_wrong_tx_to": S(intent=dict(is_deploy=True, create2_salt="0x" + "0" * 64,
                                         token=None, to=FACTORY)),
    "create2_with_token": S(intent=dict(is_deploy=True, create2_salt="0x" + "0" * 64,
                                        to=FACTORY), tx=dict(to=FACTORY)),
    "create_with_intent_to": S(intent=dict(is_deploy=True, token=None)),
    "create_with_tx_to": S(intent=dict(is_deploy=True, token=None, to=None)),
    "create_with_token": S(intent=dict(is_deploy=True, to=None), tx=dict(to=None)),
    "no_destination": S(intent=dict(to=None)),
    "zero_destination": S(intent=dict(to=ZERO)),
    "burn_destination": S(intent=dict(to=DEAD)),
    # -- 4. RPC trust --------------------------------------------------------
    "rpc_not_pinned": S(pinned=False),
    "liquidity_pin_fails": S(
        intent=dict(is_liquidity_op=True, token=None, amount_raw=0),
        patches={"core.wallet.liquidity_guard.structural": lambda *a, **k: None},
        liquidity_rpc=_raise(RuntimeError("rpc down"))),
    # -- 5. simulate ---------------------------------------------------------
    "simulate_raises": S(simulate=_raise(RuntimeError("node 502"))),
    "simulate_not_ok": S(deltas=dict(ok=False, error="reverted")),
    "target_token_refused": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS, ctx=dict(
        metadata={"money_target": {"chain": "base", "address": OTHER}}),
        intent=dict(token=USDC, inflow_token=TOKEN_X),
        deltas=dict(token_deltas={USDC: -1_000, TOKEN_X: 950})),
    "target_token_check_raises": S(
        patches={"core.wallet.buy_target.simulated_acquisition_refusal":
                 _raise(RuntimeError("target db"))}),
    # -- 6. deltas -----------------------------------------------------------
    "token_unmeasured": S(deltas=dict(token_deltas={})),
    "approve_moves_funds": S(APPROVE_INTENT, APPROVE_TX, APPROVE_DELTAS,
                             deltas=dict(token_deltas={USDC: -5})),
    "send_shows_inflow": S(deltas=dict(token_deltas={USDC: 5})),
    "send_moves_nothing": S(deltas=dict(token_deltas={USDC: 0})),
    "send_moves_more": S(deltas=dict(token_deltas={USDC: -250_001})),
    "claim_drains_native": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS,
                             deltas=dict(native_delta=-ETH)),
    "claim_drains_token": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS,
                            deltas=dict(token_deltas={USDC: -1})),
    "claim_nothing_arrives": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS,
                               deltas=dict(native_delta=0)),
    "claim_below_minimum": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS,
                             deltas=dict(native_delta=ETH // 10_000)),
    "claim_token_unmeasured": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS,
                                intent=dict(min_native_inflow_wei=None, inflow_token=USDC,
                                            min_inflow_raw=100)),
    "claim_token_below": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS,
                           intent=dict(min_native_inflow_wei=None, inflow_token=USDC,
                                       min_inflow_raw=100),
                           deltas=dict(token_deltas={USDC: 50})),
    "claim_token_allowed": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS, price=2000.0,
                             intent=dict(min_native_inflow_wei=None, inflow_token=USDC,
                                         min_inflow_raw=100),
                             deltas=dict(native_delta=0, token_deltas={USDC: 150})),
    "registration_moves_native": S(REG_INTENT, REG_TX, REG_DELTAS, price=2000.0,
                                   deltas=dict(native_delta=-ETH)),
    "nft_moves_native": S(NFT_INTENT, NFT_TX, NFT_DELTAS, price=2000.0,
                          deltas=dict(native_delta=-ETH)),
    "native_allowance_op": S(intent=dict(token=None, is_allowance_op=True,
                                         expected_allowance_grants=((USDC, ROUTER, 1),),
                                         amount_raw=0)),
    "native_wrong_destination": S(NATIVE_INTENT, NATIVE_TX, NATIVE_DELTAS,
                                  tx=dict(to=OTHER)),
    "native_inflow": S(NATIVE_INTENT, NATIVE_TX, NATIVE_DELTAS, deltas=dict(native_delta=5)),
    "native_nothing": S(NATIVE_INTENT, NATIVE_TX, NATIVE_DELTAS, deltas=dict(native_delta=0)),
    "native_short": S(NATIVE_INTENT, NATIVE_TX, NATIVE_DELTAS,
                      deltas=dict(native_delta=-(ETH // 1000))),
    "native_long": S(NATIVE_INTENT, NATIVE_TX, NATIVE_DELTAS,
                     deltas=dict(native_delta=-(ETH // 10))),
    "undeclared_allowance": S(deltas=dict(allowance_deltas={(USDC, OTHER): 5})),
    "allowance_over_declared": S(APPROVE_INTENT, APPROVE_TX, APPROVE_DELTAS,
                                 deltas=dict(allowance_deltas={(USDC, ROUTER): 2_000_000})),
    # -- 6a. minimum inflow --------------------------------------------------
    "min_inflow_zero": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS, intent=dict(min_inflow_raw=0)),
    "min_inflow_no_token": S(intent=dict(min_inflow_raw=5)),
    "min_inflow_unmeasured": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS,
                               deltas=dict(token_deltas={TOKEN_X: -1_000})),
    "min_inflow_below": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS,
                          deltas=dict(token_deltas={TOKEN_X: -1_000, USDC: 10})),
    # -- 6b. event log -------------------------------------------------------
    "event_undeclared_approval": S(deltas=dict(holder_approvals=((TOKEN_X, OTHER, 9),))),
    "event_revoke_approval_ok": S(deltas=dict(holder_approvals=((TOKEN_X, OTHER, 0),
                                                                (USDC, SPENDER, 9)))),
    "event_permit2_grant": S(deltas=dict(holder_permit2_grants=((PERMIT2, USDC, OTHER, 5),))),
    "transfer_wrong_recipient": S(tx=dict(to=USDC), deltas=dict(
        holder_transfers=((USDC, OTHER, 250_000),))),
    "transfer_calldata_not_to": S(tx=dict(data=_transfer_data(OTHER, 250_000))),
    "transfer_calldata_wrong_selector": S(tx=dict(data="0x23b872dd" + "0" * 128)),
    "transfer_calldata_ok": S(tx=dict(data=_transfer_data(TO, 250_000))),
    "transfer_paid_measured": S(tx=dict(data=_transfer_data(OTHER, 250_000)), deltas=dict(
        holder_transfers=((USDC, TO, 250_000), (USDC, TO, 0)))),
    "event_undeclared_transfer": S(deltas=dict(holder_transfers=((TOKEN_X, OTHER, 5),))),
    # -- 6c/6d/6e. non-fungibles ---------------------------------------------
    "nft_undeclared_out": S(deltas=dict(holder_nft_out=((NFT, "erc721", OTHER, 1, 1),))),
    "nft_declared_not_moved": S(NFT_INTENT, NFT_TX, NFT_DELTAS, price=2000.0,
                                deltas=dict(holder_nft_out=())),
    "nft_expected_in_missing": S(NFT_INTENT, NFT_TX, NFT_DELTAS, price=2000.0,
                                 intent=dict(expected_nft_in=((NFT, "erc721", 8, 1),))),
    "nft_revoke_unmeasured": S(NFT_INTENT, NFT_TX, NFT_DELTAS, price=2000.0,
                               intent=dict(nft_approval_revokes=((NFT, 7),))),
    "erc6909_revoke_unmeasured": S(NFT_INTENT, NFT_TX, NFT_DELTAS, price=2000.0,
                                   intent=dict(erc6909_revokes=((NFT, OTHER, 1),))),
    "operator_grant_emitted": S(deltas=dict(holder_operator_grants=((NFT, OTHER, True),
                                                                    (NFT, OTHER, False)))),
    "erc6909_grant_emitted": S(deltas=dict(holder_6909_approvals=((NFT, OTHER, 1, 5),))),
    "nft_approval_emitted": S(deltas=dict(holder_nft_approvals=((NFT, ZERO, 1),
                                                                (NFT, OTHER, 2)))),
    "nft_approval_declared": S(NFT_INTENT, NFT_TX, NFT_DELTAS, price=2000.0,
                               intent=dict(nft_operator_ops=((NFT, OTHER, False),)),
                               deltas=dict(holder_nft_approvals=((NFT, OTHER, 2),))),
    # -- 6g / 6g'' / 6g'. accounts -------------------------------------------
    "nesting_refused": S(NFT_INTENT, NFT_TX, NFT_DELTAS, price=2000.0, patches={
        "core.wallet.tx_guard._nft_nesting_refusal": lambda *a, **k: "nested"}),
    "future_account_refused": S(patches={
        "core.wallet.tx_guard._future_account_refusal": lambda *a, **k: "future"}),
    "departure_refused": S(NFT_INTENT, NFT_TX, NFT_DELTAS, price=2000.0, patches={
        "core.wallet.tx_guard._nft_nesting_refusal": lambda *a, **k: None,
        "core.wallet.tx_guard._pinned_nft_departure_refusal": lambda *a, **k: "leaves"}),
    # -- 6f. registration receipt --------------------------------------------
    "registration_update_mints": S(REG_INTENT, REG_TX, REG_DELTAS, price=2000.0,
                                   intent=dict(expects_mint=False)),
    "registration_update_receives": S(REG_INTENT, REG_TX, REG_DELTAS, price=2000.0,
                                      intent=dict(expects_mint=False),
                                      deltas=dict(holder_nft_in=((REGISTRY, "erc721", OTHER,
                                                                  42, 1),))),
    "registration_update_allowed": S(REG_INTENT, REG_TX, REG_DELTAS, price=2000.0,
                                     intent=dict(expects_mint=False),
                                     deltas=dict(holder_nft_in=())),
    "registration_transfer_in": S(REG_INTENT, REG_TX, REG_DELTAS, price=2000.0,
                                  deltas=dict(holder_nft_in=((REGISTRY, "erc721", OTHER,
                                                              42, 1),))),
    "registration_mints_nothing": S(REG_INTENT, REG_TX, REG_DELTAS, price=2000.0,
                                    deltas=dict(holder_nft_in=())),
    "registration_mints_two": S(REG_INTENT, REG_TX, REG_DELTAS, price=2000.0,
                                deltas=dict(holder_nft_in=((REGISTRY, "erc721", ZERO, 42, 1),
                                                           (REGISTRY, "erc721", ZERO, 43, 1)))),
    # -- native receipt on a token sell --------------------------------------
    "native_receipt_zero_min": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS,
                                 intent=dict(inflow_token=None, min_inflow_raw=None,
                                             min_native_inflow_wei=0)),
    "native_receipt_none": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS,
                             intent=dict(inflow_token=None, min_inflow_raw=None,
                                         min_native_inflow_wei=1000)),
    "native_receipt_below": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS,
                              intent=dict(inflow_token=None, min_inflow_raw=None,
                                          min_native_inflow_wei=1000),
                              deltas=dict(native_delta=10)),
    "native_receipt_ok": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS, price=2000.0,
                           intent=dict(inflow_token=None, min_inflow_raw=None,
                                       min_native_inflow_wei=1000),
                           deltas=dict(native_delta=ETH // 10_000)),
    "unexpected_native_change": S(deltas=dict(native_delta=-ETH)),
    # -- 7. pricing ----------------------------------------------------------
    "claim_no_proxy_chain": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS, patches={
        "core.wallet.chains.get": lambda chain: None}),
    "claim_price_raises": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS,
                            price=_raise(RuntimeError("oracle"))),
    "claim_unpriced_with_fallback": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS, price=None,
                                      fallback=1.0),
    "token_price_raises": S(price=_raise(RuntimeError("oracle"))),
    "token_unpriced_no_fallback": S(price=None),
    "token_exit_fallback_priced": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS,
                                    prices={TOKEN_X: None}, fallback=0.001),
    "token_exit_fallback_raises": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS,
                                    prices={TOKEN_X: None},
                                    fallback=_raise(RuntimeError("fb"))),
    "token_exit_inflow_valued": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS,
                                  prices={TOKEN_X: None}, fallback=None),
    "token_exit_inflow_unpriced": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS,
                                    prices={TOKEN_X: None, USDC: None}, fallback=None),
    "token_native_inflow_valued": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS,
                                    prices={TOKEN_X: None, WETH: 2000.0},
                                    intent=dict(inflow_token=None, min_inflow_raw=None,
                                                min_native_inflow_wei=1000),
                                    deltas=dict(native_delta=ETH // 10_000)),
    "token_decimals_unknown": S(patches={
        "core.wallet.tx_guard._decimals_for": lambda chain, token: None}),
    "approve_priced": S(APPROVE_INTENT, APPROVE_TX, APPROVE_DELTAS, price=None),
    "native_no_proxy": S(NATIVE_INTENT, NATIVE_TX, NATIVE_DELTAS, patches={
        "core.wallet.chains.get": lambda chain: None}),
    "native_price_raises": S(NATIVE_INTENT, NATIVE_TX, NATIVE_DELTAS,
                             price=_raise(RuntimeError("oracle"))),
    "native_unpriced": S(NATIVE_INTENT, NATIVE_TX, NATIVE_DELTAS, price=None),
    "declared_grant_on_call": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS, intent=dict(
        expected_allowance_grants=((TOKEN_X, ROUTER, 1_000),)),
        deltas=dict(allowance_deltas={(TOKEN_X, ROUTER): 1_000})),
    "declared_grant_unpriced": S(SWAP_INTENT, SWAP_TX, SWAP_DELTAS, intent=dict(
        expected_allowance_grants=((OTHER, ROUTER, 10 ** 30),)),
        prices={OTHER: None}, deltas=dict(allowance_deltas={(OTHER, ROUTER): 10 ** 30})),
    "fee_priced": S(tx=dict(gas=100_000, maxFeePerGas=10 ** 9), deltas=dict(gas_used=60_000),
                    prices={WETH: 2000.0}),
    "fee_unpriced": S(tx=dict(gas=100_000, maxFeePerGas=10 ** 9), prices={WETH: None}),
    "valuation_not_finite": S(price=float("inf")),
    "over_declared_max": S(intent=dict(max_spend_usd=0.10)),
    # -- 8. PolicyGate -------------------------------------------------------
    "gate_per_tx_refused": S(gate=dict(max_per_tx_usd=0.10)),
    "gate_replay_refused": S(gate=dict(replay="k1")),
    # -- 9. approval lane ----------------------------------------------------
    "ceiling_owner_queue": S(NATIVE_INTENT, NATIVE_TX, NATIVE_DELTAS, price=2000.0,
                             intent=dict(amount_raw=ETH // 20, max_spend_usd=500.0),
                             tx=dict(value=ETH // 20), deltas=dict(native_delta=-(ETH // 20))),
    "ceiling_owner_direct": S(NATIVE_INTENT, NATIVE_TX, NATIVE_DELTAS, price=2000.0, ctx="owner",
                              intent=dict(amount_raw=ETH // 20, max_spend_usd=500.0),
                              tx=dict(value=ETH // 20), deltas=dict(native_delta=-(ETH // 20))),
}


NPM = "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1"
JOURNAL = "0x7777777777777777777777777777777777777777"
TOPIC_ENTRY = "0xea73436bac7c599269868df7096541c0c17491cbda227e7a8aa5bb075658fb39"
_TG = "core.wallet.tx_guard."
_NONE = lambda *a, **k: None  # noqa: E731

DEPLOY_INTENT = dict(chain="base", token=None, to=None, amount_raw=0, max_spend_usd=50.0,
                     is_deploy=True, idempotency_key="kd")
DEPLOY_TX = {"data": "0x6080", "value": 0, "chainId": 8453, "nonce": 3,
             "gas": 500_000, "maxFeePerGas": 1_000_000}
DEPLOY_DELTAS = dict(ok=True, native_delta=0, token_deltas={}, gas_used=400_000)
DEPLOY_OK = {"core.wallet.deploy_guard.assert_deploy": lambda *a, **k: ("FACTS", None)}

LP_INTENT = dict(chain="base", token=None, to=NPM, amount_raw=0, max_spend_usd=50.0,
                 is_liquidity_op=True, lp_outflows=((USDC, 100),), lp_position=(NPM, None),
                 lp_position_effect="mint", idempotency_key="kl")
LP_TX = {"to": NPM, "data": "0x88316456", "value": 0, "chainId": 8453,
         "gas": 300_000, "maxFeePerGas": 1_000_000}
LP_DELTAS = dict(ok=True, native_delta=0, token_deltas={USDC: -100}, gas_used=200_000,
                 holder_transfers=((USDC, NPM, 100),))
LP_OK = {"core.wallet.liquidity_guard.structural": _NONE,
         "core.wallet.liquidity_guard.verify_pins": _NONE,
         "core.wallet.liquidity_guard.assert_deltas": lambda *a, **k: 77,
         "core.wallet.liquidity_guard.price_outflows": lambda *a, **k: (3.0, True)}


def _owner_rpc(owner):
    return lambda method, params: "0x" + "0" * 24 + owner.lower()[2:]


MINT_INTENT = dict(chain="base", token=None, to=NFT, amount_raw=ETH // 1000,
                   max_spend_usd=50.0, is_collection_mint=True, mint_ids=(1,),
                   idempotency_key="km")
MINT_TX = {"to": NFT, "data": "0x40c10f19", "value": ETH // 1000, "chainId": 8453,
           "gas": 200_000, "maxFeePerGas": 1_000_000}
MINT_DELTAS = dict(ok=True, native_delta=-(ETH // 1000), token_deltas={}, gas_used=100_000,
                   holder_nft_in=((NFT, "erc721", ZERO, 1, 1),))
MINT_OK = {"core.wallet.collection_mint.structural": _NONE,
           "core.wallet.collection_mint.assert_simulation": _NONE,
           _TG + "_collection_runtime_refusal": _NONE}

REVEAL_INTENT = dict(chain="base", token=None, to=NFT, amount_raw=0, max_spend_usd=50.0,
                     is_collection_reveal=True, reveal_ids=(1,), idempotency_key="kv")
REVEAL_TX = {"to": NFT, "data": "0x1e2b6b5b", "value": 0, "chainId": 8453,
             "gas": 200_000, "maxFeePerGas": 1_000_000}
REVEAL_DELTAS = dict(ok=True, native_delta=0, token_deltas={}, gas_used=100_000)
REVEAL_OK = {"core.wallet.collection_reveal.structural": _NONE,
             "core.wallet.collection_reveal.assert_simulation": lambda *a, **k: (None, (1,), ()),
             _TG + "_collection_runtime_refusal": _NONE}

ACCOUNT_OK = {_TG + "_via_account_inner": lambda intent, tx: (
                  {"to": USDC, "data": "0xa9059cbb", "value": 0}, None),
              _TG + "_via_account_preflight": _NONE,
              _TG + "_treasury_side_refusal": _NONE}
JOURNAL_INTENT = dict(chain="base", token=None, to=JOURNAL, amount_raw=0, max_spend_usd=50.0,
                      is_journal_entry=True, via_account=ACCOUNT, idempotency_key="kj")
JOURNAL_TX = {"to": ACCOUNT, "data": "0x51945447", "value": 0, "chainId": 8453,
              "gas": 100_000, "maxFeePerGas": 1_000_000}
JOURNAL_DELTAS = dict(ok=True, native_delta=0, token_deltas={}, gas_used=50_000,
                      event_topics=((JOURNAL, TOPIC_ENTRY),))
JOURNAL_OK = {**ACCOUNT_OK,
              _TG + "_via_account_inner": lambda intent, tx: (
                  {"to": JOURNAL, "data": "0x", "value": 0}, None),
              _TG + "_journal_only_refusal": lambda *a, **k: (JOURNAL, None)}

SCENARIOS.update({
    "claim_allowance_with_grant": S(CLAIM_INTENT, CLAIM_TX, CLAIM_DELTAS, intent=dict(
        is_allowance_op=True, expected_allowance_grants=((USDC, ROUTER, 1),))),
    "registration_registry_unresolvable": S(REG_INTENT, REG_TX, REG_DELTAS, patches={
        "core.wallet.erc8004.resolve_identity_registry": _raise(KeyError("nochain"))}),
    # -- deploy (CREATE) ------------------------------------------------------
    "deploy_allowed": S(DEPLOY_INTENT, DEPLOY_TX, DEPLOY_DELTAS, price=2000.0,
                        patches=DEPLOY_OK),
    "deploy_guard_refuses": S(DEPLOY_INTENT, DEPLOY_TX, DEPLOY_DELTAS, price=2000.0, patches={
        "core.wallet.deploy_guard.assert_deploy": lambda *a, **k: (None, "refused: code")}),
    "deploy_pays_deployer": S(DEPLOY_INTENT, DEPLOY_TX, DEPLOY_DELTAS, price=2000.0,
                              patches=DEPLOY_OK, deltas=dict(native_delta=5)),
    "deploy_overspends": S(DEPLOY_INTENT, DEPLOY_TX, DEPLOY_DELTAS, price=2000.0,
                           patches=DEPLOY_OK, deltas=dict(native_delta=-ETH)),
    "deploy_no_proxy": S(DEPLOY_INTENT, DEPLOY_TX, DEPLOY_DELTAS, price=2000.0,
                         patches={**DEPLOY_OK, "core.wallet.chains.get": lambda c: None}),
    "deploy_price_raises": S(DEPLOY_INTENT, DEPLOY_TX, DEPLOY_DELTAS,
                             price=_raise(RuntimeError("oracle")), patches=DEPLOY_OK),
    "deploy_unpriced": S(DEPLOY_INTENT, DEPLOY_TX, DEPLOY_DELTAS, price=None,
                         patches=DEPLOY_OK),
    # -- liquidity -------------------------------------------------------------
    "lp_mint_allowed": S(LP_INTENT, LP_TX, LP_DELTAS, price=2000.0, patches=LP_OK),
    "lp_mint_burn_events": S(LP_INTENT, LP_TX, LP_DELTAS, price=2000.0, patches=LP_OK,
                             intent=dict(lp_position_effect="burn", lp_position=(NPM, 5)),
                             liquidity_rpc=_owner_rpc(HOLDER),
                             deltas=dict(holder_nft_out=((NPM, "erc721", ZERO, 5, 1),),
                                         holder_nft_approvals=((NPM, ZERO, 5),))),
    "lp_not_owner": S(LP_INTENT, LP_TX, LP_DELTAS, price=2000.0, patches=LP_OK,
                      intent=dict(lp_position_effect="hold", lp_position=(NPM, 5)),
                      liquidity_rpc=_owner_rpc(OTHER)),
    "lp_assert_refuses": S(LP_INTENT, LP_TX, LP_DELTAS, price=2000.0, patches={
        **LP_OK, "core.wallet.liquidity_guard.assert_deltas": _raise(ValueError("lp delta"))}),
    "lp_price_refuses": S(LP_INTENT, LP_TX, LP_DELTAS, price=2000.0, patches={
        **LP_OK, "core.wallet.liquidity_guard.price_outflows": _raise(ValueError("lp px"))}),
    "lp_fee_only_allowed": S(LP_INTENT, LP_TX, LP_DELTAS, price=2000.0, patches=LP_OK,
                             intent=dict(lp_outflows=(), lp_inflows=((USDC, 5),),
                                         lp_position_effect="hold", lp_position=(NPM, 5)),
                             liquidity_rpc=_owner_rpc(HOLDER),
                             deltas=dict(token_deltas={USDC: 5}, holder_transfers=())),
    "lp_fee_unmeasured": S(LP_INTENT, dict(LP_TX, gas=0, maxFeePerGas=0), LP_DELTAS,
                           price=2000.0, patches=LP_OK,
                           intent=dict(lp_outflows=(), lp_inflows=((USDC, 5),),
                                       lp_position_effect="hold", lp_position=(NPM, 5)),
                           liquidity_rpc=_owner_rpc(HOLDER),
                           deltas=dict(token_deltas={USDC: 5}, holder_transfers=(),
                                       gas_used=0)),
    # -- collection mint / reveal ----------------------------------------------
    "mint_allowed": S(MINT_INTENT, MINT_TX, MINT_DELTAS, price=2000.0, patches=MINT_OK),
    "mint_runtime_drift": S(MINT_INTENT, MINT_TX, MINT_DELTAS, price=2000.0, patches={
        **MINT_OK, _TG + "_collection_runtime_refusal": lambda *a, **k: "code drift"}),
    "mint_simulation_refused": S(MINT_INTENT, MINT_TX, MINT_DELTAS, price=2000.0, patches={
        **MINT_OK, "core.wallet.collection_mint.assert_simulation": lambda *a, **k: "bad"}),
    "mint_no_proxy": S(MINT_INTENT, MINT_TX, MINT_DELTAS, price=2000.0,
                       patches={**MINT_OK, "core.wallet.chains.get": lambda c: None}),
    "mint_price_raises": S(MINT_INTENT, MINT_TX, MINT_DELTAS,
                           price=_raise(RuntimeError("oracle")), patches=MINT_OK),
    "reveal_allowed": S(REVEAL_INTENT, REVEAL_TX, REVEAL_DELTAS, price=2000.0,
                        patches=REVEAL_OK),
    "reveal_simulation_refused": S(REVEAL_INTENT, REVEAL_TX, REVEAL_DELTAS, price=2000.0,
                                   patches={**REVEAL_OK,
                                            "core.wallet.collection_reveal.assert_simulation":
                                            lambda *a, **k: ("no event", (), ())}),
    # -- token-bound account + journal -----------------------------------------
    "via_account_allowed": S(intent=dict(via_account=ACCOUNT), tx=dict(to=ACCOUNT),
                             patches=ACCOUNT_OK),
    "via_account_preflight_refused": S(intent=dict(via_account=ACCOUNT), tx=dict(to=ACCOUNT),
                                       patches={**ACCOUNT_OK, _TG + "_via_account_preflight":
                                                lambda *a, **k: "not the owner"}),
    "via_account_treasury_refused": S(intent=dict(via_account=ACCOUNT), tx=dict(to=ACCOUNT),
                                      patches={**ACCOUNT_OK, _TG + "_treasury_side_refusal":
                                               lambda *a, **k: "treasury moved"}),
    "via_account_journal_leg_ok": S(intent=dict(via_account=ACCOUNT, via_account_journal=True),
                                    tx=dict(to=ACCOUNT), patches={
                                        **ACCOUNT_OK, _TG + "_strip_journal_leg":
                                        lambda intent, tx, signer: (tx, JOURNAL, None)}),
    "batch_reset_unmeasured": S(intent=dict(via_account=ACCOUNT, via_account_batch=True,
                                            to=ROUTER), tx=dict(to=ACCOUNT), patches=ACCOUNT_OK),
    "batch_reset_left_open": S(intent=dict(via_account=ACCOUNT, via_account_batch=True,
                                           to=ROUTER), tx=dict(to=ACCOUNT), patches=ACCOUNT_OK,
                               deltas=dict(allowance_after={(USDC, ROUTER): 9})),
    "batch_reset_ok": S(intent=dict(via_account=ACCOUNT, via_account_batch=True, to=ROUTER),
                        tx=dict(to=ACCOUNT), patches=ACCOUNT_OK,
                        deltas=dict(allowance_after={(USDC, ROUTER): 0})),
    "journal_allowed": S(JOURNAL_INTENT, JOURNAL_TX, JOURNAL_DELTAS, price=2000.0,
                         patches=JOURNAL_OK),
    "journal_moves": S(JOURNAL_INTENT, JOURNAL_TX, JOURNAL_DELTAS, price=2000.0,
                       patches=JOURNAL_OK, deltas=dict(native_delta=-1)),
    "journal_no_emit": S(JOURNAL_INTENT, JOURNAL_TX, JOURNAL_DELTAS, price=2000.0,
                         patches=JOURNAL_OK, deltas=dict(event_topics=())),
})


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def _price_fn(spec):
    price, prices = spec["price"], spec["prices"]
    if callable(price) and not isinstance(price, float):
        return price

    def fn(chain, addr):
        if prices is not None:
            for k, v in prices.items():
                if str(k).lower() == str(addr).lower():
                    return v
        return price
    return fn


def _run(spec, monkeypatch):
    for k, v in spec["env"].items():
        monkeypatch.setenv(k, v)
    for target, value in spec["patches"].items():
        monkeypatch.setattr(target, value)
    intent = tx_guard.TxIntent(**{**spec["base_intent"], **spec["intent"]})
    tx = {**spec["base_tx"], **spec["tx"]}
    tx = {k: v for k, v in tx.items() if v is not None}
    deltas = Deltas(**{**spec["base_deltas"], **spec["deltas"]})
    gate_kw = dict(max_per_tx_usd=1000.0, daily_cap_usd=5000.0)
    replay = spec["gate"].pop("replay", None) if spec["gate"] else None
    gate_kw.update(spec["gate"])
    gate = PolicyGate(**gate_kw)
    if replay:
        gate.check(venue="defi", amount_usd=0.01, idempotency_key=replay)
        gate.record(venue="defi", action="golden", amount_usd=0.01, counterparty=TO,
                    result_ref="r0", idempotency_key=replay)
    ctx = spec["ctx"]
    if ctx == "owner":
        ctx = _owner_ctx()
    elif isinstance(ctx, dict):
        ctx = _owner_ctx(**ctx)
    forged_fn = spec["forged_fn"]
    if forged_fn == "default":
        forged = spec["forged"]
        forged_fn = (lambda c, t: forged) if ctx is not None else None
    auto = spec["autonomous_ok"]
    autonomous_ok_fn = auto if callable(auto) else (
        None if auto is None else (lambda c, t: auto))
    halted, entry_paused = spec["halted"], spec["entry_paused"]
    fallback = spec["fallback"]
    simulate = spec["simulate"] or (lambda **_: deltas)
    return tx_guard.authorize(
        intent, tx, holder=HOLDER, gate=gate, execution_context=ctx,
        tool_self=SimpleNamespace(name="golden_tool"),
        simulate_fn=simulate,
        price_fn=_price_fn(spec),
        fallback_price_fn=(fallback if callable(fallback) else
                           None if fallback is None else (lambda c, a: fallback)),
        rpc_is_pinned_fn=lambda chain: spec["pinned"],
        halted_fn=halted if callable(halted) else (lambda: halted),
        entry_paused_fn=entry_paused if callable(entry_paused) else (lambda: entry_paused),
        forged_fn=forged_fn, autonomous_ok_fn=autonomous_ok_fn,
        liquidity_rpc=spec["liquidity_rpc"], account_rpc=spec["account_rpc"])


def _snapshot(decision) -> dict:
    out = {}
    for f in fields(decision):
        v = getattr(decision, f.name)
        if isinstance(v, dict):
            v = {str(k): v[k] for k in sorted(v, key=str)}
        elif v is not None and not isinstance(v, (bool, int, float, str)):
            v = repr(v)
        out[f.name] = v
    return out


@pytest.fixture(autouse=True)
def _deterministic_env(monkeypatch, tmp_path):
    for k in ("DEFI_AUTONOMOUS_TURN_TRADING", "DEFI_MONITOR_EXITS", "WALLET_SIGNER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEFI_AUTONOMOUS_MAX_USD", "25")
    monkeypatch.setenv("AGENT_WALLET_MAX_PER_TX_USD", "1000")
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda: OWNER)
    monkeypatch.setattr("core.instance.resolve_owner_user_id", lambda: OWNER)
    monkeypatch.setattr("core.runtime_paths.prefs_home_dir", lambda: tmp_path / "prefs")
    # The fixture tokens have no registry row; give them 18 decimals.
    real_decimals = tx_guard._decimals_for
    monkeypatch.setattr(tx_guard, "_decimals_for", lambda chain, token: (
        18 if str(token).lower() in (TOKEN_X.lower(), OTHER.lower())
        else real_decimals(chain, token)))
    # The refusal ledger is a side channel; it must never change an outcome.
    monkeypatch.setattr("core.security.refusals.record_refusal", lambda *a, **k: None)


def _load_golden() -> dict:
    return json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_decision_matches_golden(name, monkeypatch):
    got = _snapshot(_run(dict(SCENARIOS[name], gate=dict(SCENARIOS[name]["gate"])),
                         monkeypatch))
    if _REGEN_ROWS is not None:  # pragma: no cover - the manual regen tool
        _REGEN_ROWS[name] = got
        return
    golden = _load_golden()
    assert name in golden, f"no golden for {name} — regenerate (see module docstring)"
    assert got == golden[name]


def test_golden_has_no_stale_rows():
    assert sorted(_load_golden()) == sorted(SCENARIOS)


def test_golden_covers_both_outcomes():
    """A golden that only ever refuses (or only allows) pins nothing about the
    other half of the guard."""
    rows = _load_golden().values()
    assert any(r["allowed"] for r in rows)
    assert sum(1 for r in rows if not r["allowed"]) > 100
    assert any(r["lane"] == "owner_queue" for r in rows)


def _regen() -> None:  # pragma: no cover - manual tool
    rows: dict = {}

    class _Regen:
        def pytest_collection_modifyitems(self, items):
            for item in items:
                item.module._REGEN_ROWS = rows

        def pytest_sessionfinish(self):
            GOLDEN.write_text(json.dumps(rows, indent=1, sort_keys=True) + "\n")

    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider",
                          "-k", "test_decision_matches_golden"], plugins=[_Regen()]))


if __name__ == "__main__":  # pragma: no cover
    if "--regen" in sys.argv:
        _regen()
