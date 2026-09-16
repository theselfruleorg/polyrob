from types import SimpleNamespace

import pytest

from core.wallet import simulation
from tools.defi.lp_receipts import assert_fungible_receipt

HOLDER, TOKEN, NPM, OTHER = ['0x' + c * 40 for c in '1234']


def transfer(token=TOKEN, sender=OTHER, recipient=HOLDER, amount=10):
    return dict(address=token, topics=[simulation._TOPIC_TRANSFER,
        '0x' + sender[2:].rjust(64, '0'), '0x' + recipient[2:].rjust(64, '0')],
        data='0x' + amount.to_bytes(32, 'big').hex())


def intent(**kwargs):
    return SimpleNamespace(**(dict(lp_outflows=(), lp_inflows=((TOKEN, 10),),
        lp_position=(NPM, 42), lp_position_effect='hold') | kwargs))


def test_collect_uses_transfers_not_nominal_collect_event():
    assert_fungible_receipt(intent(), [transfer()], HOLDER)
    with pytest.raises(ValueError, match='below minimum'):
        assert_fungible_receipt(intent(), [transfer(amount=9)], HOLDER)


def test_undeclared_incoming_transfer_refuses():
    with pytest.raises(ValueError, match='undeclared'):
        assert_fungible_receipt(intent(), [transfer(), transfer(token=OTHER)], HOLDER)


def test_outflow_limit_and_missing_transfer():
    i = intent(lp_inflows=(), lp_outflows=((TOKEN, 10),))
    assert_fungible_receipt(i, [transfer(sender=HOLDER, recipient=OTHER)], HOLDER)
    for logs in ([], [transfer(sender=HOLDER, recipient=OTHER, amount=11)]):
        with pytest.raises(ValueError, match='outflow'):
            assert_fungible_receipt(i, logs, HOLDER)


def test_burn_approval_clear_only_for_declared_position():
    i = intent(lp_position_effect='burn')
    clear = dict(address=NPM, topics=[simulation._TOPIC_APPROVAL,
        '0x' + HOLDER[2:].rjust(64, '0'), '0x' + '0' * 64, hex(42)], data='0x')
    assert_fungible_receipt(i, [transfer(), clear], HOLDER)
    clear['topics'][3] = hex(43)
    with pytest.raises(ValueError, match='NFT approval'):
        assert_fungible_receipt(i, [transfer(), clear], HOLDER)
