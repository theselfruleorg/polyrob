"""Validate receipt identity before an RPC reply becomes a landed outcome."""
import re


def receipt_fields(receipt, tx_hash):
    """Return (status, block, gas), or None for absent/inconsistent evidence.

    Inclusion is not finality; this only prevents malformed or unrelated RPC
    responses from becoming a successful/failed receipt for this submission.
    """
    if not isinstance(receipt, dict):
        return None
    returned = receipt.get('transactionHash')
    if (not isinstance(returned, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', returned)
            or returned.lower() != tx_hash.lower()):
        return None
    values = [receipt.get(key) for key in ('status', 'blockNumber', 'gasUsed')]
    if any(not isinstance(value, str) or not re.fullmatch(r'0x[0-9a-fA-F]{1,64}', value) for value in values):
        return None
    status, block, gas = (int(value, 16) for value in values)
    if status not in (0, 1):
        return None
    return status, block, gas
