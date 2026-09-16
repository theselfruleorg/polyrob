"""Read-only chain evidence for unresolved submissions.

Never signs, broadcasts, records a debit, or releases a reservation. A finalized
receipt still needs correct accounting; missing status is never proof of failure.
Venue attempts without an external identifier require venue-specific evidence.
"""
from dataclasses import asdict, dataclass
import re


@dataclass(frozen=True)
class RecoveryEvidence:
    reference: str
    chain: str
    outcome: str
    detail: str
    block: int | None = None
    fee_raw: int | None = None
    reservation_retained: bool = True
    fee_scope: str | None = None


def _quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{1,64}', value):
        raise ValueError('invalid RPC quantity')
    return int(value, 16)


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', value):
        raise ValueError('invalid RPC hash')
    return value.lower()


def _evm_evidence(row, call):
    from core.wallet import chains
    chain = row['chain'].removeprefix('treasury:')
    config = chains.get(chain)
    if config is None or config.family != 'evm':
        raise ValueError('unsupported configured chain')
    reference = _hash(row['tx_hash'])
    holder = row['holder'].lower()
    if not re.fullmatch(r'0x[0-9a-f]{40}', holder):
        raise ValueError('invalid stored holder')
    nonce = int(row['nonce'])
    if nonce < 0 or _quantity(call(chain, 'eth_chainId', [])) != config.chain_id:
        raise ValueError('RPC chain differs from configured chain')
    receipt = call(chain, 'eth_getTransactionReceipt', [reference])
    if receipt is None:
        return RecoveryEvidence(reference, row['chain'], 'unknown', 'No receipt; keep the reservation and do not retry blindly.')
    if _hash(receipt['transactionHash']) != reference or receipt['from'].lower() != holder:
        raise ValueError('receipt identity mismatch')
    transaction = call(chain, 'eth_getTransactionByHash', [reference])
    if (not transaction or _hash(transaction['hash']) != reference
            or transaction['from'].lower() != holder or _quantity(transaction['nonce']) != nonce):
        raise ValueError('transaction identity/nonce mismatch')
    number = _quantity(receipt['blockNumber'])
    block_hash = _hash(receipt['blockHash'])
    if (_quantity(transaction['blockNumber']) != number
            or _hash(transaction['blockHash']) != block_hash):
        raise ValueError('transaction and receipt block mismatch')
    canonical = call(chain, 'eth_getBlockByNumber', [hex(number), False])
    if not canonical or _quantity(canonical['number']) != number or _hash(canonical['hash']) != block_hash:
        return RecoveryEvidence(reference, row['chain'], 'reorg_or_inconsistent',
                                'Receipt block is not canonical at this RPC; keep the reservation.', number)
    status = _quantity(receipt['status'])
    if status not in (0, 1):
        raise ValueError('invalid receipt status')
    fee = _quantity(receipt['gasUsed']) * _quantity(receipt['effectiveGasPrice'])
    finalized = call(chain, 'eth_getBlockByNumber', ['finalized', False])
    if not finalized or _quantity(finalized['number']) < number:
        return RecoveryEvidence(reference, row['chain'], 'included_unfinalized',
                                'Receipt observed; finality is not established. Keep the reservation.', number, fee,
                                fee_scope='execution_gas_only_excludes_l1_and_blob_fees')
    # For a receipt at the finalized head, bind its hash as well as its number.
    if _quantity(finalized['number']) == number and _hash(finalized['hash']) != block_hash:
        raise ValueError('finalized head disagrees with receipt block')
    return RecoveryEvidence(reference, row['chain'], 'finalized_success' if status else 'finalized_failed',
                            'Configured RPC reports finality; reconcile effects and fee accounting before release.', number, fee,
                            fee_scope='execution_gas_only_excludes_l1_and_blob_fees')


def _solana_evidence(row, call):
    reference = row['tx_hash']
    # Decode locally to validate the case-sensitive signature before using RPC.
    from solders.signature import Signature
    Signature.from_string(reference)
    response = call('getSignatureStatuses', [[reference], {'searchTransactionHistory': True}])
    values = response.get('value') if isinstance(response, dict) else None
    if not isinstance(values, list) or len(values) != 1:
        raise ValueError('invalid signature status response')
    status = values[0]
    if status is None:
        return RecoveryEvidence(reference, 'solana', 'unknown', 'Signature absent from RPC history; this does not prove no settlement.')
    if status.get('confirmationStatus') != 'finalized':
        return RecoveryEvidence(reference, 'solana', 'included_unfinalized',
                                'Finality not established at the configured RPC; keep the reservation.')
    transaction = call('getTransaction', [reference, {
        'commitment': 'finalized', 'encoding': 'json', 'maxSupportedTransactionVersion': 0}])
    if transaction is None:
        raise ValueError('finalized transaction unavailable')
    message = transaction['transaction']['message']
    if (transaction['transaction']['signatures'][0] != reference
            or message['accountKeys'][0] != row['holder']
            or message['recentBlockhash'] != row['nonce']):
        raise ValueError('finalized transaction identity mismatch')
    slot = transaction['slot']
    meta = transaction['meta']
    if (type(slot) is not int or slot < 0 or type(status['slot']) is not int or status['slot'] != slot
            or type(meta['fee']) is not int or meta['fee'] < 0
            or meta['err'] != status['err']):
        raise ValueError('inconsistent finalized transaction metadata')
    return RecoveryEvidence(reference, 'solana', 'finalized_success' if meta['err'] is None else 'finalized_failed',
                            'Configured RPC reports finality; reconcile effects and fee accounting before release.', slot, meta['fee'],
                            fee_scope='transaction_fee_excludes_transfers_and_rent')


def inspect_submission(row, *, evm_rpc=None, solana_rpc=None):
    reference, chain = row['tx_hash'], row['chain']
    if reference.startswith(('attempt:', 'signing:')):
        return RecoveryEvidence(reference, chain, 'operator_evidence_required',
                                'No chain identifier was durably bound; do not infer non-submission or retry.')
    try:
        if chain == 'solana':
            if solana_rpc is None:
                from core.wallet.solana_onchain import _rpc as solana_rpc
            return _solana_evidence(row, solana_rpc)
        if evm_rpc is None:
            from core.wallet.onchain import _rpc, rpc_url_for_chain
            def evm_rpc(name, method, params):
                return _rpc(rpc_url_for_chain(name), method, params, timeout=5)
        return _evm_evidence(row, evm_rpc)
    except Exception as exc:
        # Never relay RPC exception text: endpoints can contain credentials.
        return RecoveryEvidence(reference, chain, 'unknown',
                                f'Could not verify chain evidence ({type(exc).__name__}); reservation retained.')


def recovery_report(*, evm_rpc=None, solana_rpc=None):
    from core.wallet.submission_journal import unresolved
    rows = unresolved()  # Damaged storage raises; never return a false clean report.
    return [asdict(inspect_submission(row, evm_rpc=evm_rpc, solana_rpc=solana_rpc)) for row in rows]


def main():
    """Emit read-only evidence; return nonzero while recovery work remains."""
    import json
    try:
        rows = recovery_report()
    except Exception as exc:
        print(json.dumps({'status': 'unavailable', 'error_type': type(exc).__name__,
                          'detail': 'Journal unreadable; spending must remain blocked.'}))
        return 2
    print(json.dumps({'status': 'blocked' if rows else 'no_unresolved_submissions',
                      'submissions': rows}, indent=2))
    return 1 if rows else 0


if __name__ == '__main__':
    raise SystemExit(main())
