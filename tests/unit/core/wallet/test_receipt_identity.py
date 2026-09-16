from types import SimpleNamespace

import pytest

from core.wallet.broadcast.evm import EvmRail

HASH = '0x' + 'a' * 64


@pytest.mark.parametrize('field,value', [
    ('transactionHash', None), ('transactionHash', '0x' + 'b' * 64),
    ('status', None), ('status', '0x2'), ('status', 'garbage'), ('status', 1.2),
    ('blockNumber', None), ('blockNumber', '-0x1'), ('gasUsed', None),
])
def test_invalid_receipt_is_pending_and_cannot_be_labeled_landed(monkeypatch, field, value):
    receipt = dict(transactionHash=HASH, status='0x1', blockNumber='0x10', gasUsed='0x5208')
    receipt[field] = value
    rail = EvmRail('base', SimpleNamespace(address='0x' + 'c' * 40))
    monkeypatch.setattr(rail, '_rpc', lambda *a: receipt)
    assert rail.await_receipt(HASH, timeout=0).status == 'pending'


def test_invalid_receipt_can_be_followed_by_matching_receipt(monkeypatch):
    replies = iter([{'status': '0x1'}, dict(transactionHash=HASH, status='0x1',
                                          blockNumber='0x10', gasUsed='0x5208')])
    rail = EvmRail('base', SimpleNamespace(address='0x' + 'c' * 40))
    monkeypatch.setattr(rail, '_rpc', lambda *a: next(replies))
    result = rail.await_receipt(HASH, timeout=1, poll_interval=0)
    assert result.status == 'success'
    assert result.block_number == 16
