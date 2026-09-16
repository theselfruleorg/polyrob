from types import SimpleNamespace

import pytest

from core.wallet.authority import owner_refusal, turn_refusal, money_tool, money_action
from core.exec_identity import set_exec_identity, reset_exec_identity


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setenv('POLYROB_OWNER_USER_ID', 'owner')


@pytest.mark.parametrize('uid', [None, '', 'customer', 'local'])
def test_role_or_authentication_is_not_wallet_ownership(uid):
    assert owner_refusal(uid)
    assert turn_refusal(SimpleNamespace(user_id=uid, role='owner'))


def test_owner_identity_and_ambient_mismatch():
    ctx = SimpleNamespace(user_id='owner')
    assert turn_refusal(ctx) is None
    token = set_exec_identity('customer', 's')
    try:
        assert turn_refusal(ctx)
        assert turn_refusal(None)
    finally:
        reset_exec_identity(token)


def test_money_aliases_and_read_namespace():
    assert money_tool('mcp:defi_trade')
    assert money_action('defi_trade_transfer')
    assert money_action('polymarket_place_order')
    assert not money_action('polymarket_data_market')


def test_real_guard_refuses_customer_before_simulation():
    from core.wallet.tx_guard import authorize, TxIntent
    def unexpected(**kw):
        raise AssertionError('must refuse before network work')
    result = authorize(TxIntent(chain='base', token=None, to='0x' + '1'*40,
        amount_raw=1, max_spend_usd=1), {}, holder='0x'+'2'*40, gate=None,
        execution_context=SimpleNamespace(user_id='customer', role='orchestrator'),
        simulate_fn=unexpected, forged_fn=lambda *a: False)
    assert not result.allowed and 'owner' in result.reason


def test_money_hook_precedes_queue_and_requires_context():
    from tools.controller.wallet_authority import make_wallet_authority_hook
    controller = SimpleNamespace(user_id='owner', get_action_details=lambda n: None)
    hook = make_wallet_authority_hook(controller)
    assert hook('defi_trade_transfer', {}, None)
    assert hook('defi_trade_transfer', {}, SimpleNamespace(user_id='customer'))
    assert hook('defi_trade_transfer', {}, SimpleNamespace(user_id='owner')) is None
    assert hook('defi_data_quote', {}, None) is None
