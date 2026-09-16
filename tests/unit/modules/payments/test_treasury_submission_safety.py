from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from eth_utils import keccak
from web3 import Web3

from core.wallet import submission_journal as journal
from modules.payments.treasury_sweeper import TreasurySweeper

ADDRESS = '0x' + '1' * 40
RAW = b'synthetic signed sweep'
HASH = '0x' + keccak(RAW).hex()


@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))
    deposit = dict(id=1, user_id='test', chain='ethereum', user_address=ADDRESS,
                   deposit_address=ADDRESS, token_symbol='ETH', amount_usd=100)
    account = SimpleNamespace(address=ADDRESS, sign_transaction=Mock(
        return_value=SimpleNamespace(raw_transaction=RAW)))
    eth = SimpleNamespace(chain_id=1, gas_price=100,
        get_balance=Mock(return_value=10**9),
        get_transaction_count=Mock(return_value=3),
        send_raw_transaction=Mock(return_value=bytes.fromhex(HASH[2:])),
        wait_for_transaction_receipt=Mock(return_value={
            'status': 1, 'transactionHash': bytes.fromhex(HASH[2:])}),
        account=SimpleNamespace(from_key=Mock(return_value=account)))
    w3 = SimpleNamespace(eth=eth, to_hex=Web3.to_hex)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)))
    wallet = SimpleNamespace(get_private_key_for_user_id=Mock(return_value='test-only'))
    sweeper = TreasurySweeper(db, wallet, SimpleNamespace(
        treasury_address='0x'+'2'*40, ethereum_rpc_url='https://example.invalid'))
    monkeypatch.setattr('web3.Web3', Mock(return_value=w3))
    return sweeper, w3, account, deposit


@pytest.mark.asyncio
async def test_hash_is_durable_before_send_and_fee_matches_signed_value(rig):
    sweeper, w3, account, deposit = rig
    def send(raw):
        assert journal.unresolved()[0]['tx_hash'] == HASH
        assert raw == RAW
        return bytes.fromhex(HASH[2:])
    w3.eth.send_raw_transaction.side_effect = send
    await sweeper._sweep_deposit(deposit)
    tx = account.sign_transaction.call_args.args[0]
    assert tx['chainId'] == 1
    assert tx['value'] + tx['gas'] * tx['gasPrice'] == 10**9
    w3.eth.get_transaction_count.assert_called_once_with(ADDRESS, 'pending')
    sweeper.db.execute.assert_awaited_once()
    assert not journal.unresolved()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['send_timeout', 'hash_mismatch', 'receipt_timeout',
                                     'reverted', 'receipt_mismatch', 'database', 'missing_row'])
async def test_uncertain_or_unbooked_sweep_blocks_retry(rig, failure):
    sweeper, w3, account, deposit = rig
    if failure == 'send_timeout':
        w3.eth.send_raw_transaction.side_effect = TimeoutError('reply lost')
    elif failure == 'hash_mismatch':
        w3.eth.send_raw_transaction.return_value = b'wrong'
    elif failure == 'receipt_timeout':
        w3.eth.wait_for_transaction_receipt.side_effect = TimeoutError('pending')
    elif failure == 'reverted':
        w3.eth.wait_for_transaction_receipt.return_value['status'] = 0
    elif failure == 'receipt_mismatch':
        w3.eth.wait_for_transaction_receipt.return_value['transactionHash'] = b'wrong'
    elif failure == 'database':
        sweeper.db.execute.side_effect = OSError('disk full')
    elif failure == 'missing_row':
        sweeper.db.execute.return_value.rowcount = 0
    await sweeper._sweep_deposit(deposit)
    assert journal.unresolved()[0]['tx_hash'] == HASH
    await sweeper._sweep_deposit(deposit)
    assert w3.eth.send_raw_transaction.call_count == 1
    assert account.sign_transaction.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['chain', 'derived_address', 'deposit_address'])
async def test_wrong_chain_or_source_cannot_sign(rig, mismatch):
    sweeper, w3, account, deposit = rig
    if mismatch == 'chain':
        w3.eth.chain_id = 11155111
    elif mismatch == 'derived_address':
        account.address = '0x'+'3'*40
    else:
        deposit['deposit_address'] = '0x'+'3'*40
    await sweeper._sweep_deposit(deposit)
    account.sign_transaction.assert_not_called()
    w3.eth.send_raw_transaction.assert_not_called()


@pytest.mark.asyncio
async def test_storage_failure_cannot_broadcast(rig, monkeypatch):
    sweeper, w3, account, deposit = rig
    monkeypatch.setattr(journal, 'prepare', Mock(side_effect=OSError('disk full')))
    await sweeper._sweep_deposit(deposit)
    account.sign_transaction.assert_not_called()
    w3.eth.send_raw_transaction.assert_not_called()
    sweeper.db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_signing_failure_keeps_prepared_sweep_reserved(rig):
    sweeper, w3, account, deposit = rig
    def sign(tx):
        pending = journal.unresolved()
        assert pending[0]['state'] == 'reserved'
        assert pending[0]['chain'] == 'treasury:ethereum'
        raise RuntimeError('signing outcome unknown')
    account.sign_transaction.side_effect = sign
    await sweeper._sweep_deposit(deposit)
    assert journal.unresolved()[0]['state'] == 'reserved'
    w3.eth.send_raw_transaction.assert_not_called()
    sweeper.db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_path_uses_same_interlock(rig):
    sweeper, w3, account, deposit = rig
    deposit['token_symbol'] = 'USDC'
    transfer = Mock()
    transfer.build_transaction.side_effect = lambda tx: dict(tx, to='token-contract')
    contract = SimpleNamespace(functions=SimpleNamespace(
        balanceOf=Mock(return_value=SimpleNamespace(call=Mock(return_value=100))),
        transfer=Mock(return_value=transfer)))
    w3.eth.contract = Mock(return_value=contract)
    w3.eth.send_raw_transaction.side_effect = TimeoutError('lost')
    await sweeper._sweep_deposit(deposit)
    assert journal.unresolved()[0]['tx_hash'] == HASH
    await sweeper._sweep_deposit(deposit)
    assert w3.eth.send_raw_transaction.call_count == 1
    assert account.sign_transaction.call_args.args[0]['chainId'] == 1


@pytest.mark.asyncio
async def test_cancellation_cannot_release_inflight_submission(rig):
    import asyncio
    import threading

    sweeper, w3, account, deposit = rig
    receipt_started = threading.Event()
    release_receipt = threading.Event()
    confirmed = w3.eth.wait_for_transaction_receipt.return_value
    def receipt(*args, **kwargs):
        receipt_started.set()
        assert release_receipt.wait(5)
        return confirmed
    w3.eth.wait_for_transaction_receipt.side_effect = receipt
    task = asyncio.create_task(sweeper._sweep_deposit(deposit))
    try:
        assert await asyncio.to_thread(receipt_started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert journal.unresolved()[0]['tx_hash'] == HASH
        sweeper.db.execute.assert_not_awaited()
    finally:
        release_receipt.set()
