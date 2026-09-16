from types import SimpleNamespace
from eth_utils import keccak
import pytest

from core.wallet import submission_journal as J
from core.wallet.broadcast.evm import EvmRail
from core.wallet.policy import PolicyGate
from core.wallet.audit_sink import JsonlAuditSink

RAW = b'synthetic signed transaction bytes'
HASH = '0x' + keccak(RAW).hex()


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))


def rail(monkeypatch, response=None):
    signer = SimpleNamespace(address='0x'+'1'*40, sign_transaction=lambda tx: RAW)
    r = EvmRail(chain='base', signer=signer)
    monkeypatch.setattr(r, 'preflight', lambda: (True, None))
    def rpc(method, params):
        assert J.unresolved()[0]['tx_hash'] == HASH
        if response is None:
            raise TimeoutError('response lost after acceptance')
        return response
    monkeypatch.setattr(r, '_rpc', rpc)
    return r


@pytest.mark.parametrize('response', [None, '0xwrong', HASH])
def test_broadcast_keeps_local_hash_and_persists_before_rpc(monkeypatch, response):
    r = rail(monkeypatch, response)
    assert r.sign_and_send({'chainId': 8453, 'nonce': 1}) == HASH
    assert J.unresolved()[0]['nonce'] == '1'
    with pytest.raises(ValueError, match='unaccounted'):
        r.sign_and_send({'chainId': 8453, 'nonce': 2})
    # A newly constructed gate cannot spend through an unresolved submission.
    assert not PolicyGate(max_per_tx_usd=10).check(venue='x402', amount_usd=1, idempotency_key='next').allowed


def test_failed_journal_prevents_network_send(monkeypatch):
    r = rail(monkeypatch)
    monkeypatch.setattr(J, 'prepare', lambda *a, **kw: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        r.sign_and_send({'chainId': 8453, 'nonce': 1})


def test_only_durable_charge_releases_interlock(tmp_path):
    J.prepare(HASH, 'base', '0x'+'1'*40, 1)
    kwargs = dict(venue='defi', action='transfer', amount_usd=2,
        counterparty=None, idempotency_key='k', result_ref=HASH)
    PolicyGate(max_per_tx_usd=10).record(**kwargs)
    assert J.unresolved()
    sink = JsonlAuditSink(str(tmp_path/'audit.jsonl'))
    PolicyGate(max_per_tx_usd=10, audit_sink=sink).record(**kwargs)
    assert not J.unresolved()
    assert JsonlAuditSink(str(tmp_path/'audit.jsonl'))[0]['amount_usd'] == 2


def test_corrupt_journal_refuses_spending():
    path = J.journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('corrupt')
    assert not PolicyGate(max_per_tx_usd=10).check(venue='defi', amount_usd=1, idempotency_key='x').allowed


def test_attempt_released_only_by_durable_accounting(tmp_path):
    ref = J.prepare_attempt('hyperliquid', 'owner', 5)
    assert J.unresolved()[0]['tx_hash'] == ref
    kwargs = dict(venue='hyperliquid', action='order', amount_usd=5,
                  counterparty=None, idempotency_key=None, result_ref='venue-order-id',
                  submission_ref=ref)
    PolicyGate(max_per_tx_usd=10).record(**kwargs)
    assert J.unresolved()
    sink = JsonlAuditSink(str(tmp_path / 'audit.jsonl'))
    PolicyGate(max_per_tx_usd=10, audit_sink=sink).record(**kwargs)
    assert not J.unresolved()
    assert JsonlAuditSink(str(tmp_path / 'audit.jsonl'))[0]['submission_ref'] == ref
