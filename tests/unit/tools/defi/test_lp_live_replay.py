"""Offline replay of public Ethereum reads captured at block 0x18c943e.

Principal is checked exactly against decreaseLiquidity. Nominal fee growth is
not equated to collect: the recorded actual returns include periphery rounding.
"""
import json
from pathlib import Path

import pytest

from core.wallet import dex_registry
from tools.defi import lp_abi as A, lp_reads as R


@pytest.fixture
def recording():
    path = Path(__file__).resolve().parents[3] / 'fixtures/liquidity/ethereum-live-positions-2026-09-16.json'
    capture = json.loads(path.read_text())
    def key(method, params):
        return json.dumps([method, params], sort_keys=True).lower()
    answers = {key(c['method'], c['params']): c['result'] for c in capture['calls']}
    def rpc(method, params):
        pinned = [capture['block'] if p == 'latest' else p for p in params]
        return answers[key(method, pinned)]  # Missing calls fail; never use network.
    return capture, rpc


@pytest.mark.parametrize('token_id', [1, 5, 6])
def test_live_position_principal_and_actual_collect(recording, token_id):
    capture, rpc = recording
    expected = next(p for p in capture['checked'] if p['token_id'] == token_id)
    dex_registry.verify_pins(rpc, capture['chain'], 'v3')
    position = R.position(rpc, capture['chain'], token_id)
    assert (position.amount0, position.amount1) == tuple(expected['amounts'])
    assert (position.fees0, position.fees1) == tuple(expected['quoted_fees'])
    npm = dex_registry.resolve_position_manager(capture['chain'], 'v3')
    owner = R.view(rpc, npm, A.NPM_OWNER_OF, [token_id])
    actual = R.collectible_fees(rpc, capture['chain'], token_id, owner)
    assert actual == tuple(expected['fees'])
    assert all(0 <= a <= q for a, q in zip(actual, expected['quoted_fees']))
    if token_id == 6:
        assert position.fees0 == 278 and actual[0] == 270


def test_collect_failure_is_unavailable():
    def rpc(*args):
        raise RuntimeError('offline')
    with pytest.raises(R.LpReadError, match='collectible fees unavailable: offline'):
        R.collectible_fees(rpc, 'ethereum', 1, '0x' + '1' * 40)
