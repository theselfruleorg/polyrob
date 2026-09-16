import json

from core.status_liquidity import liquidity_section
from core.status_snapshot import _guarded, STATE_UNAVAILABLE
from tests.unit.core.test_status_collectibles import _db


def test_unread_is_not_empty_and_activity_is_tenant_scoped(tmp_path):
    _db(tmp_path, [(1, 'wallet_spend', 'rob', json.dumps({'action': 'lp_add', 'asset': 'erc721:npm:42'})),
                   (2, 'wallet_spend', 'other', json.dumps({'action': 'lp_collect'}))])
    sec = liquidity_section('rob', str(tmp_path))
    assert sec.data['held'] is None
    assert len(sec.data['moved']) == 1
    assert 'not read' in sec.lines[0]


def test_failed_enumeration_is_unavailable_not_empty(tmp_path):
    _db(tmp_path, [])
    def fail(**kw):
        raise RuntimeError('RPC unavailable')
    sec = liquidity_section('rob', str(tmp_path), fail)
    assert sec.data['held'] is None
    assert 'unavailable' in sec.lines[0]
    assert liquidity_section('rob', str(tmp_path), lambda **kw: []).data['held'] == []


def test_missing_ledger_is_unavailable(tmp_path):
    assert _guarded('liquidity', liquidity_section, 'rob', str(tmp_path)).state == STATE_UNAVAILABLE
