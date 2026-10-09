"""CR-L01 (2026-09-23): a node-REJECTED broadcast is not-sent, not unknown.

Before, "nonce too low" / "insufficient funds" / "underpriced" were treated
like a lost response: the verb waited 120 s for a receipt that could never
come, booked the full USD and released the journal. A JSON-RPC error that
proves the node refused the bytes now raises BroadcastError and frees the
interlock; a transport failure — and "already known" — stays unknown.
"""
from types import SimpleNamespace

import pytest
from eth_utils import keccak

from core.wallet import onchain
from core.wallet import submission_journal as J
from core.wallet.broadcast.evm import BroadcastError, EvmRail, definitive_rejection

RAW = b'synthetic signed transaction bytes'
HASH = '0x' + keccak(RAW).hex()


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))


def _rail(monkeypatch, exc):
    signer = SimpleNamespace(address='0x' + '1' * 40, sign_transaction=lambda tx: RAW)
    r = EvmRail(chain='base', signer=signer)
    monkeypatch.setattr(r, 'preflight', lambda: (True, None))

    def rpc(method, params):
        raise exc
    monkeypatch.setattr(r, '_rpc', rpc)
    return r


@pytest.mark.parametrize('msg', [
    "{'code': -32000, 'message': 'nonce too low'}",
    "{'code': -32000, 'message': 'insufficient funds for gas * price + value'}",
    "{'code': -32000, 'message': 'replacement transaction underpriced'}",
    "{'code': -32000, 'message': 'transaction underpriced'}",
])
def test_a_definitive_rejection_is_not_sent_and_frees_the_interlock(monkeypatch, msg):
    r = _rail(monkeypatch, onchain.RpcError(f"eth_sendRawTransaction: {msg}"))
    with pytest.raises(BroadcastError, match='rejected') as info:
        r.sign_and_send({'chainId': 8453, 'nonce': 1})
    assert J.unresolved() == []
    # A market condition, tagged where it was raised: the run is not refusal-tainted.
    assert info.value.precondition is True


@pytest.mark.parametrize('exc', [
    TimeoutError('response lost after acceptance'),
    onchain.RpcError("eth_sendRawTransaction: {'message': 'already known'}"),
    onchain.RpcError("eth_sendRawTransaction: response carried no result"),
])
def test_an_ambiguous_failure_stays_unknown(monkeypatch, exc):
    r = _rail(monkeypatch, exc)
    assert r.sign_and_send({'chainId': 8453, 'nonce': 1}) == HASH
    assert J.unresolved()[0]['tx_hash'] == HASH


def test_the_classifier_needs_an_rpc_error_response():
    assert definitive_rejection(OSError('nonce too low')) is None
    assert definitive_rejection(onchain.RpcError('x: nonce too low')) == 'nonce too low'
