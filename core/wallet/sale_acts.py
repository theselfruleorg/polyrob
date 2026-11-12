"""The seller's on-chain sale acts of an ERC-6551P collection — the calls, the events and the
reads, with no policy (proposal 111 §8 W7; 115 W7).

Pure: this module imports nothing of the wallet (so it stays out of the guard's import cycle;
the hex encoding is plain). The guard half — the shape gate, the RPC
pins, the measured result and the SALE HOLD — is ``core/wallet/tx_guard_account.py``; the verbs
are ``tools/agent_nft/sale_ready.py``; the holdings watch reads :func:`read_sale_ready`.

The contract of record (the first deployment, polyrob-desk ``contracts/src``):

* ``readyToSell(uint256 id)`` (``kit/ERC6551PCollection.sol:68-79``) — only the token's OWN
  account (``msg.sender == accountOf(id)``), as the last leg of an account batch (the account
  allows only ``JournalLog.log`` legs after it, ``account/PolyrobAccount.sol:243-251``).
  Refused when the owner is itself an ERC-6551 account (``SellerIsAccount``). Records
  ``keccak256(owner, account.state())`` and emits ``SaleReady(id, owner, state)``.
* ``cancelReady(uint256 id)`` (``:82-87``) — the token owner or its account: ends readiness.
  It emits no event of the standard (a collection may add its own in ``_onReadyChanged``).
* ``saleReadyOf(uint256 id)`` (``:91-96``) — true while the record matches the current owner
  and the account's current ``state()``. The account changes ``state()`` at the start of EVERY
  batch (``PolyrobAccount.sol:277``), so any act through the account ends readiness; every
  transfer ends it too (``:149-158``).
* the market (``sale/PolyrobMarket.sol:254-261``): ``cancel(uint256 salt)`` marks one salt of
  the caller used (``SaltCancelled(signer, salt)``); ``incrementNonce()`` ends every order the
  caller signed with the old nonce (``NonceIncremented(signer, nonce)``). Both move nothing.
* ``approve(market, id)`` on the collection (ERC-721; 111 W9a): the market moves a sold token
  with ``transferFrom`` (``_ticketTransfer``, ``PolyrobMarket.sol:350-355``), so a listing or an
  accept needs the seller's approval of the PINNED market for that ONE token — never
  ``setApprovalForAll``. It emits ``Approval(owner, market, id)``; a transfer clears it (OZ 5
  ``ERC721._update``, no event).

Each selector and topic is a literal; ``tests/unit/core/wallet/test_sale_acts.py`` computes
every one from its signature.
"""
from __future__ import annotations

from typing import Optional, Tuple

READY_SIGNATURE = "readyToSell(uint256)"
READY_SELECTOR = "0x4b8f6ae4"
CANCEL_READY_SIGNATURE = "cancelReady(uint256)"
CANCEL_READY_SELECTOR = "0x80437f23"
SALE_READY_OF_SELECTOR = "0x4ff8e4ab"       # saleReadyOf(uint256)
MARKET_CANCEL_SIGNATURE = "cancel(uint256)"
MARKET_CANCEL_SELECTOR = "0x40e58ee5"
INCREMENT_NONCE_SIGNATURE = "incrementNonce()"
INCREMENT_NONCE_SELECTOR = "0x627cdcb9"
NONCES_SELECTOR = "0x7ecebe00"              # nonces(address)
USED_SALT_SELECTOR = "0x122a506f"           # usedSalt(address,uint256)

TOPIC_SALE_READY = "0x4ef63b94134cfa8f21eec15212fc0523727b15f6354701560f6ce3e625d64a66"  # SaleReady(uint256,address,uint256)
TOPIC_SALT_CANCELLED = "0x3f631d9dfdb456a7b703180fac8bd1567e04e713d4be91e55fbfbe8b47f6780b"  # SaltCancelled(address,uint256)
TOPIC_NONCE_INCREMENTED = "0xa82a649bbd060c9099cd7b7326e2b0dc9e9af0836480e0f849dc9eaa79710b3b"  # NonceIncremented(address,uint256)
TOPIC_SOLD = "0x114ac8ea6e195fce183853d5744958b4900cb955d588f0a38b04cdf80f56f746"  # Sold(address,uint256,address,address,address,uint256,uint256)
APPROVE_SIGNATURE = "approve(address,uint256)"
APPROVE_SELECTOR = "0x095ea7b3"
GET_APPROVED_SELECTOR = "0x081812fc"        # getApproved(uint256)
TOPIC_APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"  # Approval(address,address,uint256)

#: The acts (``TxIntent`` carries none of these names: the guard recognises the act by its
#: pinned target and its calldata, and the intent declares ``is_nft_op`` with nothing to move).
KIND_READY = "ready"
KIND_UNREADY = "unready"
KIND_CANCEL_SALT = "cancel_salt"
KIND_CANCEL_ALL = "cancel_all"
#: ``approve(market, id)``: the pinned market may move this ONE token (a listing, an accept).
KIND_APPROVE = "approve_market"

#: The verbs that may make these calls (named in a refusal).
SALE_ACT_VERBS = ("agent_nft_sale_ready, agent_nft_sale_unready, agent_nft_sale_cancel_listing, "
                  "agent_nft_sale_list and agent_nft_sale_accept")

#: The sale hold (111 §8.4, finding B4): the refusal of an automatic act through a ready account.
HOLD_TEXT = ("the account is ready for sale: an act through it ends the sale readiness; the "
             "owner must ask or unready first (agent_nft_sale_unready)")
#: The note an owner-asked act through a ready account carries.
HOLD_NOTE = ("this act through the account ENDS its sale readiness (a plain marketplace fill "
             "then reverts SaleNotReady); agent_nft_sale_ready makes it ready again")
HOLD_UNREAD_NOTE = ("the account's sale readiness could not be read: if it was ready for sale, "
                    "this act ends it")

# The ERC-6551 registry's clone (173 bytes: header ‖ implementation ‖ tail ‖ footer) — the
# collection's own test of "an ERC-6551 account" (kit/ERC6551PCollection.sol:181-190).
_CLONE_LEN = 173
_CLONE_HEAD = "363d3d373d3d3d363d73"
_CLONE_TAIL = "5af43d82803e903d91602b57fd5bf3"


def _uint_arg(selector: str, value: int) -> str:
    return selector + format(int(value), "064x")


def encode_ready(token_id: int) -> str:
    return _uint_arg(READY_SELECTOR, token_id)


def encode_cancel_ready(token_id: int) -> str:
    return _uint_arg(CANCEL_READY_SELECTOR, token_id)


def encode_sale_ready_of(token_id: int) -> str:
    return _uint_arg(SALE_READY_OF_SELECTOR, token_id)


def encode_market_cancel(salt: int) -> str:
    return _uint_arg(MARKET_CANCEL_SELECTOR, salt)


def encode_approve(spender: str, token_id: int) -> str:
    return (APPROVE_SELECTOR + "0" * 24 + str(spender).lower().removeprefix("0x")
            + format(int(token_id), "064x"))


def encode_get_approved(token_id: int) -> str:
    return _uint_arg(GET_APPROVED_SELECTOR, token_id)


def encode_increment_nonce() -> str:
    return INCREMENT_NONCE_SELECTOR


def encode_nonces(who: str) -> str:
    return NONCES_SELECTOR + "0" * 24 + str(who).lower().removeprefix("0x")


def encode_used_salt(who: str, salt: int) -> str:
    return USED_SALT_SELECTOR + "0" * 24 + str(who).lower().removeprefix("0x") + format(int(salt), "064x")


def decode_uint_call(data: str, selector: str) -> int:
    """The one uint256 argument of *selector*'s calldata. Raises ValueError unless *data* is
    EXACTLY that canonical encoding (selector + one word)."""
    text = str(data or "").lower()
    if not text.startswith(selector) or len(text) != 10 + 64:
        raise ValueError(f"the calldata is not the canonical {selector}(uint256) encoding")
    return int(text[10:], 16)


def collection_act(data: str):
    """``(kind, token_id, None)`` for ``readyToSell(id)`` / ``cancelReady(id)`` calldata,
    ``(KIND_APPROVE, (spender, token_id), None)`` for ``approve(spender != 0, id)``,
    ``(None, None, None)`` for any other selector (``approve(0, id)`` — a revoke — included),
    ``(None, None, why)`` for a malformed one."""
    text = str(data or "").lower()
    if text.startswith(APPROVE_SELECTOR):
        if len(text) != 10 + 128 or int(text[10:34], 16):
            return None, None, f"the calldata is not the canonical {APPROVE_SIGNATURE} encoding"
        spender = "0x" + text[34:74]
        if int(spender, 16) == 0:
            return None, None, None
        return KIND_APPROVE, (spender, int(text[74:], 16)), None
    for kind, sel in ((KIND_READY, READY_SELECTOR), (KIND_UNREADY, CANCEL_READY_SELECTOR)):
        if text.startswith(sel):
            try:
                return kind, decode_uint_call(text, sel), None
            except ValueError as exc:
                return None, None, str(exc)
    return None, None, None


def market_act(data: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """``(KIND_CANCEL_SALT, salt, None)`` | ``(KIND_CANCEL_ALL, None, None)`` | ``(None, None,
    why)`` — the market takes no other call from this build."""
    text = str(data or "").lower()
    if text == INCREMENT_NONCE_SELECTOR:
        return KIND_CANCEL_ALL, None, None
    if text.startswith(MARKET_CANCEL_SELECTOR):
        try:
            return KIND_CANCEL_SALT, decode_uint_call(text, MARKET_CANCEL_SELECTOR), None
        except ValueError as exc:
            return None, None, str(exc)
    return None, None, (f"the calldata ({text[:10] or 'none'}) is neither "
                        f"{MARKET_CANCEL_SIGNATURE} nor {INCREMENT_NONCE_SIGNATURE} — this build "
                        f"calls the pinned market only to cancel its own orders")


def _word(raw) -> int:
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) < 66:
        raise ValueError(f"unreadable word {raw!r}")
    return int(raw[2:66], 16)


def read_sale_ready(rpc, collection: str, token_id: int) -> bool:
    """``saleReadyOf(id)`` on the collection. Raises when unreadable (never "not ready")."""
    value = _word(rpc("eth_call", [{"to": collection, "data": encode_sale_ready_of(token_id)},
                                   "latest"]))
    if value not in (0, 1):
        raise ValueError(f"saleReadyOf({token_id}) answered {value}, not a bool")
    return value == 1


def read_nonce(rpc, market: str, who: str) -> int:
    return _word(rpc("eth_call", [{"to": market, "data": encode_nonces(who)}, "latest"]))


def read_approved(rpc, collection: str, token_id: int) -> str:
    """``getApproved(id)`` (lowercase). Raises when unreadable."""
    return "0x" + format(_word(rpc("eth_call", [{"to": collection,
                                                 "data": encode_get_approved(token_id)},
                                                "latest"])), "064x")[-40:]


def read_used_salt(rpc, market: str, who: str, salt: int) -> bool:
    return _word(rpc("eth_call", [{"to": market, "data": encode_used_salt(who, salt)},
                                  "latest"])) == 1


def is_erc6551_clone(code: str) -> bool:
    """True when *code* (0x hex) is an ERC-6551 registry clone of ANY implementation — the
    collection's ``SellerIsAccount`` test."""
    raw = str(code or "").lower().removeprefix("0x")
    return (len(raw) == 2 * _CLONE_LEN and raw[:20] == _CLONE_HEAD
            and raw[60:90] == _CLONE_TAIL)


def topic_word(address: str) -> str:
    return "0x" + "0" * 24 + str(address).lower().removeprefix("0x")
