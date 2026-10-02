"""W3 (core handoff) — the two revoke shapes the agent-NFT "empty approval table" claim needs.

* single-token ERC-721: ``approve(address(0), id)`` — ``TxIntent.nft_approval_revokes``;
* Permit2: ``approve(token, spender, 0, 0)`` or ``lockdown`` — ``TxIntent.permit2_revokes``.

Each is DECLARED and then HELD to the simulation (the event must be emitted), exactly like a
declared NFT move (6d). Both are exits: an owner entry-pause does not block a revoke.
"""
import pytest

from core.wallet import erc6551, simulation, tx_guard
from core.wallet.policy import PolicyGate
from core.wallet.simulation import Deltas

HOLDER = "0x1111111111111111111111111111111111111111"
NFT = "0x4444444444444444444444444444444444444444"
TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SPENDER = "0x5555555555555555555555555555555555555555"
ZERO = "0x0000000000000000000000000000000000000000"
P2 = erc6551.PERMIT2


def _run(intent, deltas, tx, *, entry_paused=False):
    return tx_guard.authorize(
        intent, tx, holder=HOLDER, gate=PolicyGate(max_per_tx_usd=100.0, daily_cap_usd=1000.0),
        execution_context=None, simulate_fn=lambda **_: deltas, price_fn=lambda c, a: 1.0,
        rpc_is_pinned_fn=lambda c: True, halted_fn=lambda: False,
        entry_paused_fn=lambda: entry_paused, forged_fn=lambda c, t: False,
        account_rpc=lambda m, p: "0x")


def _tx(to, data):
    return {"to": to, "data": data, "value": 0, "chainId": 8453, "nonce": 1, "gas": 80_000,
            "maxFeePerGas": 10 ** 8}


# --- single-token ERC-721 ------------------------------------------------------------------

def _nft_revoke(**kw):
    base = dict(chain="base", token=None, to=NFT, amount_raw=0, max_spend_usd=5.0, idempotency_key="r",
                is_nft_op=True, nft_approval_revokes=((NFT, 7),))
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _cleared(token_id=7, contract=NFT):
    return Deltas(ok=True, native_delta=0, gas_used=40_000,
                  holder_nft_approvals=((contract.lower(), ZERO, token_id),))


def test_a_measured_single_token_revoke_is_authorized():
    d = _run(_nft_revoke(), _cleared(), _tx(NFT, erc6551.encode_erc721_revoke(7)))
    assert d.allowed is True, d.reason


@pytest.mark.parametrize("deltas", [
    Deltas(ok=True, native_delta=0, gas_used=40_000),                         # nothing emitted
    _cleared(token_id=8),                                                     # another token
    _cleared(contract="0x" + "46" * 20),                                      # another contract
])
def test_a_declared_single_token_revoke_that_is_not_measured_refuses(deltas):
    d = _run(_nft_revoke(), deltas, _tx(NFT, erc6551.encode_erc721_revoke(7)))
    assert d.allowed is False and "does not do what the intent says" in d.reason, d.reason


def test_a_single_token_revoke_must_be_an_nft_op():
    d = _run(_nft_revoke(is_nft_op=False, amount_raw=1), _cleared(), _tx(NFT, erc6551.encode_erc721_revoke(7)))
    assert d.allowed is False and "is_nft_op" in d.reason


def test_a_revoke_that_also_grants_is_still_refused():
    deltas = Deltas(ok=True, native_delta=0, gas_used=40_000,
                    holder_nft_approvals=((NFT.lower(), ZERO, 7), (NFT.lower(), SPENDER.lower(), 8)))
    d = _run(_nft_revoke(), deltas, _tx(NFT, "0x"))
    assert d.allowed is False and "UNDECLARED Approval" in d.reason


# --- Permit2 ------------------------------------------------------------------------------

def _p2_revoke(**kw):
    base = dict(chain="base", token=TOKEN, to=P2, amount_raw=0, max_spend_usd=5.0, idempotency_key="p",
                is_allowance_op=True, permit2_revokes=((TOKEN, SPENDER),))
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _p2_zeroed(amount=0, emitter=P2, spender=SPENDER):
    return Deltas(ok=True, native_delta=0, token_deltas={TOKEN: 0}, gas_used=40_000,
                  holder_permit2_grants=((emitter.lower(), TOKEN.lower(), spender.lower(), amount),))


def test_a_measured_permit2_revoke_is_authorized():
    d = _run(_p2_revoke(), _p2_zeroed(), _tx(P2, erc6551.encode_permit2_revoke(TOKEN, SPENDER)))
    assert d.allowed is True, d.reason


@pytest.mark.parametrize("deltas, needle", [
    (Deltas(ok=True, native_delta=0, token_deltas={TOKEN: 0}, gas_used=1), "no zero"),
    (_p2_zeroed(emitter="0x" + "77" * 20), "no zero"),        # a look-alike event from elsewhere
    (_p2_zeroed(spender="0x" + "66" * 20), "no zero"),        # another spender
    (_p2_zeroed(amount=5), "Permit2 Approval of 5"),         # a GRANT, not a revoke
])
def test_a_permit2_revoke_that_is_not_measured_refuses(deltas, needle):
    d = _run(_p2_revoke(), deltas, _tx(P2, erc6551.encode_permit2_revoke(TOKEN, SPENDER)))
    assert d.allowed is False and needle in d.reason, d.reason


@pytest.mark.parametrize("kw, to, needle", [
    (dict(to=SPENDER), P2, "pinned Permit2"),
    (dict(), SPENDER, "pinned Permit2"),
    (dict(is_allowance_op=False, amount_raw=1), P2, "allowance operation"),
    (dict(expected_allowance_grants=((TOKEN, SPENDER, 1),)), P2, "grants nothing"),
    (dict(token="0x" + "88" * 20), P2, "one of the tokens"),
])
def test_permit2_revoke_structure(kw, to, needle):
    d = _run(_p2_revoke(**kw), _p2_zeroed(), _tx(to, erc6551.encode_permit2_revoke(TOKEN, SPENDER)))
    assert d.allowed is False and needle in d.reason, d.reason


def test_the_simulation_reads_a_permit2_lockdown_as_a_zero():
    log = {"address": P2, "topics": ["0x89b1add15eff56b3dfe299ad94e01f2b52fbcb80ae1a3baea6ae8c04cb2b98a4",
                                     "0x" + "00" * 12 + HOLDER[2:]],
           "data": "0x" + "00" * 12 + TOKEN[2:].lower() + "00" * 12 + SPENDER[2:]}
    ev = simulation._holder_events([log], HOLDER)
    assert ev.permit2_grants == ((P2.lower(), TOKEN.lower(), SPENDER.lower(), 0),)


# --- both are exits ------------------------------------------------------------------------

def test_revokes_pass_the_owner_entry_pause():
    d = _run(_nft_revoke(), _cleared(), _tx(NFT, "0x"), entry_paused=True)
    assert d.allowed is True, d.reason
    d = _run(_p2_revoke(), _p2_zeroed(), _tx(P2, "0x"), entry_paused=True)
    assert d.allowed is True, d.reason
    assert tx_guard.intent_is_risk_reducing(_nft_revoke())
    assert not tx_guard.intent_is_risk_reducing(_nft_revoke(nft_out=((NFT, "erc721", 1, 1),)))
