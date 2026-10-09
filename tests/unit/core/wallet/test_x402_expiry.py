"""An expired, unsettled x402 authorization resolves from chain state.

A paid server that answers a second 402 or drops the connection leaves an
``attempt:`` row that refuses every money rail. After ``validBefore`` the token's
``authorizationState`` decides it; any doubt keeps the row (fail closed).
"""
import json
import os

import pytest

from core.wallet import submission_journal as sj
from core.wallet import x402_expiry as xe
from core.wallet.onchain import USDC_BASE_MAINNET

AUTHORIZER = "0x" + "1" * 40
NONCE = "0x" + "ab" * 32
VALID_BEFORE = 1_000_000


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return str(tmp_path)


def _row(key="x402:https://paid.example:2.0"):
    ref = sj.prepare_attempt("x402", AUTHORIZER, 2.0, idempotency_key=key)
    sj.bind_x402_authorization(ref, authorizer=AUTHORIZER, nonce=NONCE,
                               valid_before=VALID_BEFORE, asset=USDC_BASE_MAINNET,
                               network="eip155:8453")
    return ref


def _chain(*, latest_used=False, final_used=False, final_ts=VALID_BEFORE + 100,
           chain_id=8453, fail=None):
    calls = []

    def call(chain, method, params):
        calls.append((chain, method, params))
        if fail == method:
            raise OSError("rpc down https://key@rpc")
        if method == "eth_chainId":
            return hex(chain_id)
        if method == "eth_getBlockByNumber":
            return {"number": hex(77), "timestamp": hex(final_ts)}
        if method == "eth_call":
            req, block = params
            assert req["to"] == USDC_BASE_MAINNET.lower()
            assert req["data"] == ("0xe94a0102" + AUTHORIZER[2:].rjust(64, "0") + NONCE[2:])
            used = latest_used if block == "latest" else final_used
            return "0x" + ("0" * 63) + ("1" if used else "0")
        raise AssertionError(method)
    call.calls = calls
    return call


def _audit(home):
    with open(os.path.join(home, "wallet", "audit.jsonl"), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_an_unused_authorization_past_finality_releases_at_zero_and_frees_the_key(home):
    ref = _row()
    (out,) = xe.resolve_expired(call=_chain(), now=VALID_BEFORE + 600, data_dir=home)
    assert out.outcome == "expired_unused" and out.booked_usd == 0.0
    assert sj.unresolved(home) == []
    (entry,) = _audit(home)
    assert entry["never_sent"] is True and entry["submission_ref"] == ref
    # The request was proven unpaid, so its own replay key stays usable.
    assert entry["idempotency_key"] == f"operator_release:{ref}"
    sj.prepare("0x" + "cd" * 32, "base", AUTHORIZER, "1")          # rails unblocked


def test_a_used_authorization_books_the_signed_ceiling_under_its_replay_key(home):
    ref = _row()
    (out,) = xe.resolve_expired(call=_chain(latest_used=True), now=VALID_BEFORE + 600,
                                data_dir=home)
    assert out.outcome == "used" and out.booked_usd == 2.0
    (entry,) = _audit(home)
    assert entry["amount_usd"] == 2.0 and entry["never_sent"] is False
    assert entry["idempotency_key"] == "x402:https://paid.example:2.0"
    assert sj.unresolved(home) == [] and ref


@pytest.mark.parametrize("kwargs, now", [
    ({}, VALID_BEFORE + 5),                                   # not yet expired
    ({"final_ts": VALID_BEFORE - 1}, VALID_BEFORE + 600),     # finality behind validBefore
])
def test_a_row_that_could_still_settle_is_kept(home, kwargs, now):
    _row()
    (out,) = xe.resolve_expired(call=_chain(**kwargs), now=now, data_dir=home)
    assert out.outcome == "pending" and len(sj.unresolved(home)) == 1


@pytest.mark.parametrize("kwargs", [
    {"fail": "eth_call"}, {"fail": "eth_getBlockByNumber"}, {"chain_id": 1},
])
def test_any_read_doubt_keeps_the_row_and_never_relays_rpc_text(home, kwargs):
    _row()
    (out,) = xe.resolve_expired(call=_chain(**kwargs), now=VALID_BEFORE + 600, data_dir=home)
    assert out.outcome == "unverifiable" and "key@" not in out.detail
    assert len(sj.unresolved(home)) == 1


def test_a_row_without_terms_stays_operator_only(home):
    sj.prepare_attempt("x402", AUTHORIZER, 2.0, idempotency_key="k")
    assert xe.resolve_expired(call=_chain(), now=VALID_BEFORE + 600, data_dir=home) == []
    assert len(sj.unresolved(home)) == 1


def test_terms_bind_only_to_a_prepared_x402_attempt_once(home):
    ref = _row()
    with pytest.raises(ValueError):
        sj.bind_x402_authorization(ref, authorizer=AUTHORIZER, nonce=NONCE,
                                   valid_before=VALID_BEFORE, asset=USDC_BASE_MAINNET,
                                   network="base")
    with pytest.raises(ValueError):
        sj.bind_x402_authorization("attempt:x", authorizer="0xnot", nonce=NONCE,
                                   valid_before=VALID_BEFORE, asset=USDC_BASE_MAINNET,
                                   network="base")


def test_a_wrong_asset_is_never_read_as_usdc(home):
    ref = sj.prepare_attempt("x402", AUTHORIZER, 2.0, idempotency_key="k2")
    sj.bind_x402_authorization(ref, authorizer=AUTHORIZER, nonce=NONCE,
                               valid_before=VALID_BEFORE, asset="0x" + "9" * 40,
                               network="base")
    (out,) = xe.resolve_expired(call=_chain(), now=VALID_BEFORE + 600, data_dir=home)
    assert out.outcome == "unverifiable" and len(sj.unresolved(home)) == 1
