"""Recovery evidence never releases an uncertain or unaccounted submission."""
import copy
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.wallet import submission_journal as journal
from core.wallet.submission_recovery import inspect_submission, recovery_report

HASH = '0x' + 'a' * 64
BLOCK = '0x' + 'b' * 64
ADDRESS = '0x' + 'c' * 40
ROW = dict(tx_hash=HASH, chain='base', holder=ADDRESS, nonce='3', state='prepared')


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv('POLYROB_DATA_DIR', str(tmp_path))


@pytest.fixture
def evm():
    data = {
        'eth_chainId': '0x2105',
        'eth_getTransactionReceipt': dict(transactionHash=HASH, blockHash=BLOCK, blockNumber='0x10',
                                         status='0x1', gasUsed='0x5208', effectiveGasPrice='0x2', **{'from': ADDRESS}),
        'eth_getTransactionByHash': dict(hash=HASH, nonce='0x3', blockHash=BLOCK, blockNumber='0x10', **{'from': ADDRESS}),
        'canonical': dict(number='0x10', hash=BLOCK),
        'finalized': dict(number='0x10', hash=BLOCK),
    }
    def call(chain, method, params):
        assert chain == 'base'
        assert method in ('eth_chainId', 'eth_getTransactionReceipt', 'eth_getTransactionByHash', 'eth_getBlockByNumber')
        if method == 'eth_getBlockByNumber':
            return copy.deepcopy(data['finalized' if params[0] == 'finalized' else 'canonical'])
        return copy.deepcopy(data[method])
    return data, call


@pytest.mark.parametrize('status,outcome', [('0x1', 'finalized_success'), ('0x0', 'finalized_failed')])
@pytest.mark.parametrize('chain', ['base', 'treasury:base'])
def test_finalized_evm_evidence_includes_fee_and_retains_reservation(evm, status, outcome, chain):
    data, call = evm
    data['eth_getTransactionReceipt']['status'] = status
    result = inspect_submission(dict(ROW, chain=chain), evm_rpc=call)
    assert result.outcome == outcome
    assert result.block == 16
    assert result.fee_raw == 42000
    assert result.fee_scope == 'execution_gas_only_excludes_l1_and_blob_fees'
    assert result.reservation_retained


@pytest.mark.parametrize('case', ['chain', 'receipt_hash', 'sender', 'nonce', 'transaction_hash', 'transaction_block', 'finalized_hash', 'invalid_status'])
def test_mismatched_evm_evidence_remains_unknown(evm, case):
    data, call = evm
    if case == 'chain':
        data['eth_chainId'] = '0x1'
    elif case == 'receipt_hash':
        data['eth_getTransactionReceipt']['transactionHash'] = BLOCK
    elif case == 'sender':
        data['eth_getTransactionReceipt']['from'] = '0x' + 'd' * 40
    elif case == 'nonce':
        data['eth_getTransactionByHash']['nonce'] = '0x4'
    elif case == 'transaction_hash':
        data['eth_getTransactionByHash']['hash'] = BLOCK
    elif case == 'transaction_block':
        data['eth_getTransactionByHash']['blockHash'] = HASH
    elif case == 'finalized_hash':
        data['finalized']['hash'] = HASH
    else:
        data['eth_getTransactionReceipt']['status'] = '0x2'
    result = inspect_submission(ROW, evm_rpc=call)
    assert result.outcome == 'unknown'
    assert result.reservation_retained


@pytest.mark.parametrize('case,outcome', [('missing', 'unknown'), ('reorg', 'reorg_or_inconsistent'),
                                         ('no_finality', 'included_unfinalized'), ('early', 'included_unfinalized')])
def test_missing_and_unfinalized_evm_receipts_are_never_treated_as_failure(evm, case, outcome):
    data, call = evm
    if case == 'missing':
        data['eth_getTransactionReceipt'] = None
    elif case == 'reorg':
        data['canonical']['hash'] = HASH
    elif case == 'no_finality':
        data['finalized'] = None
    else:
        data['finalized']['number'] = '0xf'
    assert inspect_submission(ROW, evm_rpc=call).outcome == outcome


@pytest.fixture
def solana():
    from solders.signature import Signature
    from solders.hash import Hash
    from solders.pubkey import Pubkey
    signature, holder, blockhash = str(Signature.default()), str(Pubkey.default()), str(Hash.default())
    row = dict(tx_hash=signature, chain='solana', holder=holder, nonce=blockhash, state='prepared')
    data = {
        'getSignatureStatuses': {'value': [{'slot': 50, 'confirmationStatus': 'finalized', 'err': None}]},
        'getTransaction': {'slot': 50, 'meta': {'fee': 5000, 'err': None},
                           'transaction': {'signatures': [signature], 'message': {
                               'accountKeys': [holder], 'recentBlockhash': blockhash}}},
    }
    def call(method, params):
        assert method in ('getSignatureStatuses', 'getTransaction')
        if method == 'getSignatureStatuses':
            assert params == [[signature], {'searchTransactionHistory': True}]
        else:
            assert params[0] == signature and params[1]['commitment'] == 'finalized'
        return copy.deepcopy(data[method])
    return row, data, call


@pytest.mark.parametrize('failed', [False, True])
def test_solana_finality_requires_matching_transaction_and_includes_fee(solana, failed):
    row, data, call = solana
    if failed:
        data['getSignatureStatuses']['value'][0]['err'] = {'InstructionError': [0, 'Custom']}
        data['getTransaction']['meta']['err'] = {'InstructionError': [0, 'Custom']}
    result = inspect_submission(row, solana_rpc=call)
    assert result.outcome == ('finalized_failed' if failed else 'finalized_success')
    assert result.fee_raw == 5000
    assert result.fee_scope == 'transaction_fee_excludes_transfers_and_rent'
    assert result.block == 50
    assert result.reservation_retained


@pytest.mark.parametrize('case', ['missing_transaction', 'signature', 'sender', 'blockhash', 'slot', 'fee', 'error'])
def test_solana_inconsistent_evidence_remains_unknown(solana, case):
    row, data, call = solana
    tx = data['getTransaction']
    if case == 'missing_transaction':
        data['getTransaction'] = None
    elif case == 'signature':
        tx['transaction']['signatures'][0] = 'mismatch'
    elif case == 'sender':
        tx['transaction']['message']['accountKeys'][0] = 'mismatch'
    elif case == 'blockhash':
        tx['transaction']['message']['recentBlockhash'] = 'mismatch'
    elif case == 'slot':
        tx['slot'] = 49
    elif case == 'fee':
        tx['meta']['fee'] = -1
    else:
        tx['meta']['err'] = 'inconsistent'
    assert inspect_submission(row, solana_rpc=call).outcome == 'unknown'


@pytest.mark.parametrize('status,outcome', [(None, 'unknown'), ({'confirmationStatus': 'confirmed'}, 'included_unfinalized')])
def test_absent_or_early_solana_status_keeps_reservation(solana, status, outcome):
    row, data, call = solana
    data['getSignatureStatuses']['value'] = [status]
    assert inspect_submission(row, solana_rpc=call).outcome == outcome


@pytest.mark.parametrize('reference', ['attempt:abc', 'signing:abc'])
def test_attempt_without_chain_identifier_never_contacts_rpc(reference):
    rpc = Mock(side_effect=AssertionError('no chain identifier'))
    result = inspect_submission(dict(ROW, tx_hash=reference), evm_rpc=rpc, solana_rpc=rpc)
    assert result.outcome == 'operator_evidence_required'
    assert result.reservation_retained
    rpc.assert_not_called()


def test_exception_does_not_disclose_rpc_credentials():
    rpc = Mock(side_effect=RuntimeError('https://user:secret@example.invalid'))
    result = inspect_submission(ROW, evm_rpc=rpc)
    assert result.outcome == 'unknown'
    assert 'secret' not in result.detail


def test_full_report_does_not_mutate_journal_or_marker_even_on_success(evm):
    journal.prepare(HASH, 'base', ADDRESS, 3)
    path = journal.journal_path()
    marker = Path(str(path) + '.hwm')
    before = path.read_bytes(), marker.read_bytes(), journal.unresolved()
    report = recovery_report(evm_rpc=evm[1])
    assert report[0]['outcome'] == 'finalized_success'
    assert report[0]['reservation_retained']
    assert (path.read_bytes(), marker.read_bytes(), journal.unresolved()) == before


def test_storage_damage_cannot_report_a_clean_wallet():
    journal.prepare(HASH, 'base', ADDRESS, 3)
    journal.journal_path().unlink()
    with pytest.raises(ValueError):
        recovery_report()


def test_module_cli_keeps_unbound_reservation_and_uses_no_rpc(capsys):
    from core.wallet.submission_recovery import main
    reference = journal.reserve_signing('base', ADDRESS, 3)
    assert main() == 1
    output = capsys.readouterr().out
    assert reference in output
    assert 'operator_evidence_required' in output
    assert journal.unresolved()[0]['tx_hash'] == reference
