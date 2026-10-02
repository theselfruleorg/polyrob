"""068 third review (Codex R3-1 .. R3-4).

The decided target rule: under a declared ``target_token`` the run acquires NO
non-canonical token other than the target, on any chain. Canonical assets
(USDC, wrapped native, native) move freely; a token whose NET movement is <= 0
(a refund inside a sell) is not an acquisition; risk-reducing intents (LP
remove/collect, a revoke, a claim) are exempt from the inflow check.
"""
import fcntl
import inspect
import os
from types import SimpleNamespace

import pytest

from core.wallet import buy_target, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
TARGET = "0xbBa60AB93Fc409b1A34371CBF6c3173795Ed2c7e"
OTHER = "0x357A04366240aa3c9d916Aa0F15c3033686C9007"
NFT = "0x4444444444444444444444444444444444444444"
HOLDER = "0x2222222222222222222222222222222222222222"
CALLEE = "0x3333333333333333333333333333333333333333"
TRANSFER = buy_target._TOPIC_TRANSFER
SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
MEME = "MEMEMEMEMEMEMEMEMEMEMEMEMEMEMEMEMEMEMEMEMEMe"


def _ctx(chain="base", address=TARGET):
    return SimpleNamespace(role="orchestrator", is_sub_agent=False, user_id="rob",
                           metadata={buy_target.METADATA_KEY: {"chain": chain,
                                                               "address": address}})


def _word(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


def _transfer(contract, amount, *, inbound=True):
    frm, to = (CALLEE, HOLDER) if inbound else (HOLDER, CALLEE)
    return {"address": contract.lower(), "topics": [TRANSFER, _word(frm), _word(to)],
            "data": hex(amount)}


def _deltas(**kw):
    base = dict(ok=True, native_delta=0, token_deltas={}, allowance_deltas={},
                gas_used=90_000)
    base.update(kw)
    return Deltas(**base)


def _backstop(d, **kw):
    return buy_target.simulated_acquisition_refusal(_ctx(), chain="base", deltas=d,
                                                    holder=HOLDER, **kw)


# ---- R3-1: canonical is working capital; every non-target acquisition refuses

def test_codex_r3_1_target_plus_weth_is_allowed_by_design():
    d = _deltas(token_deltas={USDC: -1_000_000, TARGET: 1},
                logs=(_transfer(TARGET, 1), _transfer(WETH, 10 ** 15)))
    assert _backstop(d) is None


def test_an_extra_non_canonical_token_in_the_same_tx_refuses():
    d = _deltas(token_deltas={USDC: -1_000_000, TARGET: 1},
                logs=(_transfer(TARGET, 1), _transfer(OTHER, 5)))
    why = _backstop(d)
    assert why and OTHER.lower() in why


def test_usdc_to_weth_under_a_target_is_allowed_at_the_verb():
    assert buy_target.acquisition_refusal(_ctx(), chain="base", token_out=WETH,
                                          token_in=USDC) is None
    assert buy_target.acquisition_refusal(_ctx(), chain="base", token_out=OTHER,
                                          token_in=USDC)


def test_solana_extra_mint_in_the_simulation_refuses():
    ctx = _ctx(chain="solana", address=WSOL)
    assert buy_target.net_inflow_refusal(
        ctx, chain="solana", net_moves={SOL_USDC: -5_000_000, WSOL: 10}) is None
    why = buy_target.net_inflow_refusal(
        ctx, chain="solana", net_moves={SOL_USDC: -5_000_000, WSOL: 10, MEME: 100_000_000})
    assert why and MEME in why


def test_solana_swap_checks_every_simulated_mint_before_broadcast():
    from tools.defi.trade_tool import DefiTradeTool
    src = inspect.getsource(DefiTradeTool.solana_swap)
    assert src.index("net_inflow_refusal") < src.index("_solana_send")
    assert src.index("_solana_simulate(") < src.index("net_inflow_refusal")


# ---- R3-2: net movement, and risk-reducing intents -------------------------

def test_codex_r3_2_a_refund_inside_a_sell_is_not_an_acquisition():
    # measured: sold 1,000,000, 100,000 came back — net -900,000
    d = _deltas(token_deltas={OTHER: -900_000, USDC: 2_000_000},
                logs=(_transfer(OTHER, 1_000_000, inbound=False),
                      _transfer(OTHER, 100_000), _transfer(USDC, 2_000_000)))
    assert _backstop(d) is None


def test_a_log_only_refund_nets_out_too():
    d = _deltas(logs=(_transfer(OTHER, 1_000_000, inbound=False),
                      _transfer(OTHER, 100_000)))
    assert _backstop(d) is None


def test_an_nft_in_and_out_of_the_same_tx_is_not_kept():
    d = _deltas(holder_nft_in=((NFT.lower(), "erc721", CALLEE.lower(), 7, 1),),
                holder_nft_out=((NFT.lower(), "erc721", CALLEE.lower(), 7, 1),))
    assert _backstop(d) is None


def test_risk_reducing_intents_are_exempt():
    d = _deltas(logs=(_transfer(OTHER, 5_000),),
                holder_nft_in=((NFT.lower(), "erc721", CALLEE.lower(), 7, 1),))
    assert _backstop(d)
    assert _backstop(d, risk_reducing=True) is None


@pytest.mark.parametrize("intent_kw,expected", [
    (dict(is_liquidity_op=True, lp_outflows=(), lp_inflows=((OTHER, 5_000),)), True),
    (dict(is_liquidity_op=True, lp_outflows=((OTHER, 1),)), False),   # lp_add
    (dict(is_claim=True), True),
    (dict(is_allowance_op=True, expected_allowance_grants=()), True),  # revoke
    (dict(is_allowance_op=True, expected_allowance_grants=((OTHER, CALLEE, 1),)), False),
    (dict(), False),
])
def test_which_intents_are_risk_reducing(intent_kw, expected):
    intent = tx_guard.TxIntent(chain="base", token=None, to=CALLEE, amount_raw=0,
                               max_spend_usd=1.0, idempotency_key="k", **intent_kw)
    assert tx_guard.intent_is_risk_reducing(intent) is expected


def test_the_guard_passes_the_risk_reducing_flag_to_the_backstop():
    src = inspect.getsource(tx_guard)
    assert "risk_reducing=intent_is_risk_reducing(intent)" in src


def test_the_guard_still_target_refuses_an_ordinary_call(monkeypatch):
    intent = tx_guard.TxIntent(chain="base", token=None, to=CALLEE,
                               amount_raw=250_000_000_000_000, max_spend_usd=1.0,
                               idempotency_key="k")
    d = _deltas(native_delta=-250_000_000_000_000, logs=(_transfer(OTHER, 5_000),))
    import core.wallet.authority as auth
    monkeypatch.setattr(auth, "turn_refusal", lambda ctx: None)
    decision = tx_guard.authorize(
        intent, {"to": CALLEE, "data": "0x1249c58b", "value": 250_000_000_000_000,
                 "chainId": 8453, "nonce": 1, "gas": 120_000, "maxFeePerGas": 10 ** 9},
        holder=HOLDER, gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0),
        execution_context=_ctx(), simulate_fn=lambda **_: d,
        price_fn=lambda c, a: 1000.0, rpc_is_pinned_fn=lambda c: True,
        halted_fn=lambda: False, entry_paused_fn=lambda: False,
        forged_fn=lambda c, t: False)
    assert decision.allowed is False and "target token" in decision.reason


# ---- R3-3: a key of exactly the old truncation length ----------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return str(tmp_path)


def test_codex_r3_3_a_512_char_key_needs_the_acknowledgement(home):
    from core.wallet import submission_journal as sj
    from core.wallet.submission_release import ReleaseRefused, release_submission
    key = "x402:https://paid.example/" + "q" * (512 - len("x402:https://paid.example/"))
    assert len(key) == 512
    ref = sj.prepare_attempt("x402", "0xholder", 1.0, idempotency_key=key)
    with pytest.raises(ReleaseRefused, match="CUT SHORT"):
        release_submission(ref, data_dir=home,
                           inspect=lambda r: {"outcome": "operator_evidence_required"})
    entry = release_submission(ref, data_dir=home, no_replay_key=True,
                               reason="operator checked the merchant log",
                               inspect=lambda r: {"outcome": "operator_evidence_required"})
    assert entry["amount_usd"] >= 1.0


def test_a_511_char_key_is_trusted(home):
    from core.wallet import submission_journal as sj
    from core.wallet.submission_release import release_submission
    key = "x402:" + "k" * 506
    ref = sj.prepare_attempt("x402", "0xholder", 1.0, idempotency_key=key)
    entry = release_submission(ref, data_dir=home,
                               inspect=lambda r: {"outcome": "operator_evidence_required"})
    assert entry["idempotency_key"] == key


# ---- R3-4: one deadline, sink construction included -------------------------

def test_codex_r3_4_a_held_audit_lock_times_out_during_sink_construction(home, monkeypatch):
    from core.wallet import submission_journal as sj
    from core.wallet import submission_release as sr
    ref = sj.prepare_attempt("x402", "0xholder", 1.0, idempotency_key="x402:u:1.0")
    wallet = os.path.join(home, "wallet")
    os.makedirs(wallet, exist_ok=True)
    with open(os.path.join(wallet, "audit.jsonl"), "w") as fh:  # an existing ledger:
        fh.write("")                                             # construction loads it
    monkeypatch.setattr(sr, "RELEASE_LOCK_TIMEOUT_SEC", 0.3)
    fd = os.open(os.path.join(wallet, "audit.jsonl.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(sr.ReleaseRefused, match="spend in progress"):
            sr.release_submission(ref, data_dir=home,
                                  inspect=lambda r: {"outcome": "operator_evidence_required"})
    finally:
        os.close(fd)
    # the lock released, the same release goes through
    entry = sr.release_submission(ref, data_dir=home,
                                  inspect=lambda r: {"outcome": "operator_evidence_required"})
    assert entry["amount_usd"] >= 1.0


# ---- 068 R4 (Codex sign-off review) -------------------------------------------

def test_erc1155_net_quantity_is_what_counts():
    """R4-2: send 1 unit, receive 2 of the same non-target id → +1 kept → refused."""
    d = _deltas(holder_nft_in=((NFT.lower(), "erc1155", CALLEE.lower(), 7, 2),),
                holder_nft_out=((NFT.lower(), "erc1155", CALLEE.lower(), 7, 1),))
    assert _backstop(d)
    even = _deltas(holder_nft_in=((NFT.lower(), "erc1155", CALLEE.lower(), 7, 2),),
                   holder_nft_out=((NFT.lower(), "erc1155", CALLEE.lower(), 7, 2),))
    assert _backstop(even) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("chain,receive", [("base", WETH), ("ethereum", TARGET),
                                           ("base", USDC)])
async def test_call_receipt_under_a_target_must_be_the_target(chain, receive):
    """R4-1: a canonical (or other-chain) receipt does not classify a call."""
    from tools.defi.trade_tool import CallParams, DefiTradeTool
    tool = DefiTradeTool(wallet=None)
    res = await tool.call(CallParams(chain=chain, to=CALLEE, calldata="0x1249c58b",
                                     value=0.0001, max_spend_usd=1.0,
                                     receive_token=receive, receive_min_raw=1),
                          _ctx())
    assert res.error and "must be that token on that chain" in res.error


def test_an_erc721_outflow_does_not_cancel_an_erc1155_inflow():
    """R4-2 residue (Codex): same contract and id, different standard."""
    d = _deltas(holder_nft_in=((NFT.lower(), "erc1155", CALLEE.lower(), 7, 1),),
                holder_nft_out=((NFT.lower(), "erc721", CALLEE.lower(), 7, 1),))
    assert _backstop(d)
