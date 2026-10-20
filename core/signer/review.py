"""Signer-generated transaction detail for independent owner review."""
import hashlib
import json

#: The whole review text (50 pending rows must fit one protocol frame).
MAX_REVIEW_CHARS = 8192
_SHOWN_WORDS = 24
MAX_PENDING_APPROVALS = 50
#: A declared field longer than this is shown as its length + sha256 (an init
#: code or a calldata blob is compared by hash, never read by eye).
_MAX_DECLARED_VALUE_CHARS = 200

_ERC20_TRANSFER = "a9059cbb"        # transfer(address,uint256)
_ERC20_TRANSFER_FROM = "23b872dd"   # transferFrom(address,address,uint256)


def _short(value):
    if isinstance(value, (list, tuple)):
        return [_short(v) for v in value]
    if isinstance(value, str) and len(value) > _MAX_DECLARED_VALUE_CHARS:
        return {"chars": len(value),
                "sha256": hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()}
    return value


def _word_address(word: str) -> str:
    return "0x" + word[-40:]


def _payee(intent, tx, raw: bytes):
    """Who the signed transaction pays, decoded from the transaction itself, and
    whether that differs from what the caller declared."""
    to = str(tx.get("to") or "")
    declared = str(getattr(intent, "to", None) or "")
    selector = raw[:4].hex()
    words = [raw[i:i + 32].hex() for i in range(4, len(raw), 32)]
    payee = None
    if selector == _ERC20_TRANSFER and len(words) >= 2:
        payee = {"kind": "erc20.transfer", "token": to, "recipient": _word_address(words[0]),
                 "amount_raw": int(words[1], 16)}
    elif selector == _ERC20_TRANSFER_FROM and len(words) >= 3:
        payee = {"kind": "erc20.transferFrom", "token": to, "from": _word_address(words[0]),
                 "recipient": _word_address(words[1]), "amount_raw": int(words[2], 16)}
    elif int(tx.get("value") or 0) > 0:
        payee = {"kind": "native", "recipient": to}
    if payee is not None:
        payee["differs_from_declared_to"] = (bool(declared)
                                             and payee["recipient"].lower() != declared.lower())
    return payee


def evm_review(intent, tx, decision, *, digest, unpriced_assets):
    from core.signer.protocol import intent_to_wire
    raw = bytes.fromhex(str(tx.get('data') or '0x').removeprefix('0x'))
    is_create = not tx.get('to')
    words = [] if is_create else [raw[i:i + 32].hex() for i in range(4, len(raw), 32)]
    declared = {k: _short(v) for k, v in intent_to_wire(intent).items()
                if v not in (None, False, '', [], ()) or k in ('amount_raw', 'max_spend_usd')}
    # JSON escaping prevents caller text from forging headings or terminal controls.
    review = {
        'request_digest': digest,
        'chain': intent.chain,
        'actual_contract': tx.get('to') or '(deploy)',
        'native_value_wei': int(tx.get('value') or 0),
        'maximum_network_fee_wei': int(tx['gas']) * int(tx['maxFeePerGas']),
        'priced_risk_usd': decision.amount_usd,
        'unpriced_risk': 'UNPRICED NFT/LP principal' if unpriced_assets else None,
        'declared_intent': declared,
        'decoded_payee': None if is_create else _payee(intent, tx, raw),
        'calldata_bytes': len(raw),
        'calldata_sha256': hashlib.sha256(raw).hexdigest(),
        'selector': ('0x' + raw[:4].hex() if len(raw) >= 4 else None) if not is_create else None,
        'calldata_words': words[:_SHOWN_WORDS],
        'additional_words': max(0, len(words) - _SHOWN_WORDS),
        # A recipient can sit at ANY position: every address-shaped word past the
        # shown prefix is listed by its index, so none is hidden by the cut.
        'address_words_beyond': {str(i): _word_address(w) for i, w in enumerate(words)
                                 if i >= _SHOWN_WORDS and w[:24] == "0" * 24 and int(w, 16)},
        'guard_reason': decision.reason,
    }
    text = json.dumps(review, indent=2, sort_keys=True, ensure_ascii=True)
    if len(text) > MAX_REVIEW_CHARS:
        raise ValueError('Transaction detail exceeds signer review limit; split the request')
    return text


def x402_digest(*, chain_id: int, contract: str, payer: str, payee: str, value: int) -> str:
    """The approval key of an EIP-3009 authorization (085 W4, S-1).

    It binds the chain, the USDC contract, the payer, the payee and the exact
    value — not the nonce or the window, which the payer draws fresh on each
    attempt — so the owner's one-use grant unlocks the retry of THAT payment.
    """
    canon = json.dumps({"op": "x402.authorize", "chain_id": int(chain_id),
                        "contract": str(contract).lower(), "from": str(payer).lower(),
                        "to": str(payee).lower(), "value": int(value)},
                       sort_keys=True, separators=(",", ":"))
    return "x402:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()


def x402_review(*, chain: str, contract: str, payer: str, payee: str, value: int,
                usd: float, reason: str, digest: str) -> str:
    """The owner's review of an x402 authorization above the signer's caps."""
    review = {
        'request_digest': digest,
        'op': 'x402.authorize (EIP-3009 TransferWithAuthorization)',
        'chain': chain,
        'usdc_contract': contract,
        'payer': payer,
        'decoded_payee': {'kind': 'usdc.transferWithAuthorization', 'recipient': payee,
                          'amount_raw': int(value)},
        'priced_risk_usd': usd,
        'guard_reason': reason,
    }
    text = json.dumps(review, indent=2, sort_keys=True, ensure_ascii=True)
    if len(text) > MAX_REVIEW_CHARS:
        raise ValueError('x402 detail exceeds signer review limit')
    return text
