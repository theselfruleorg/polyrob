"""The on-chain account journal — ``JournalLog`` (polyrob-desk ``contracts/src/kit/JournalLog.sol``).

``log(bytes entry)`` emits ``Entry(address indexed account, bytes entry)`` with ``account =
msg.sender``. The contract has no owner, no storage and no value, and never reads ``entry``. An
account writes its journal by calling it THROUGH the account, as the last leg of the same
``executeBatch`` as the action the entry describes, so topic 1 is the account and only the NFT's
current owner can write. The entry bytes are the canonical JSON of one signed journal entry
(``core.wallet.nft_account.canonical``, ``sig`` included); a reader checks each signature and the
hash chain itself (:func:`core.wallet.nft_account.verified_chain`).

The address is pinned per collection in the owner's registry (``journal_log``, J2). The selector
and topic are literals (a bare install has no keccak at import); a test pins each to its signature.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

Rpc = Callable[[str, list], Any]

#: ``log(bytes)``
LOG_SELECTOR = "0x0be77f56"
#: ``Entry(address,bytes)``
TOPIC_ENTRY = "0xea73436bac7c599269868df7096541c0c17491cbda227e7a8aa5bb075658fb39"
#: The fields of one entry, exactly (``nft_account.build_entry`` + ``sig``).
ENTRY_KEYS = frozenset({"seq", "ts", "account", "chain_id", "kind", "text", "refs",
                        "positions_after", "owner", "prev", "sig"})
#: An entry on chain is calldata AND log data; bound it so a long text cannot burn the gas limit.
MAX_ENTRY_BYTES = 4096


class JournalLogError(RuntimeError):
    """The journal leg or an on-chain entry is not the shape this module writes."""


def _word(n: int) -> str:
    return int(n).to_bytes(32, "big").hex()


def encode_log(entry: bytes) -> str:
    """Calldata of ``log(entry)``."""
    entry = bytes(entry)
    padded = entry + b"\x00" * (-len(entry) % 32)
    return LOG_SELECTOR + _word(32) + _word(len(entry)) + padded.hex()


def _decode_bytes(body: bytes) -> bytes:
    if len(body) < 64 or int.from_bytes(body[:32], "big") != 32:
        raise JournalLogError("not a single dynamic bytes argument")
    n = int.from_bytes(body[32:64], "big")
    if n > MAX_ENTRY_BYTES or len(body) != 64 + n + (-n % 32):
        raise JournalLogError(f"entry length {n} does not match the encoding (max {MAX_ENTRY_BYTES})")
    out, pad = body[64:64 + n], body[64 + n:]
    if pad.strip(b"\x00"):
        raise JournalLogError("non-zero padding after the entry")
    return out


def decode_log(data: str) -> bytes:
    """The entry bytes of ``log(entry)`` calldata. Raises :class:`JournalLogError` on anything else
    (another selector, extra words, dirty padding — the encoding must be canonical)."""
    text = str(data or "").lower()
    if not text.startswith(LOG_SELECTOR):
        raise JournalLogError("the call is not JournalLog.log(bytes)")
    try:
        body = bytes.fromhex(text[len(LOG_SELECTOR):])
    except ValueError as exc:
        raise JournalLogError(f"calldata is not hex ({exc})") from exc
    return _decode_bytes(body)


def parse_entry(raw: bytes) -> Dict[str, Any]:
    """One entry from its bytes: canonical JSON with exactly :data:`ENTRY_KEYS`. Raises."""
    try:
        entry = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise JournalLogError(f"the entry is not JSON ({exc})") from exc
    if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
        raise JournalLogError("the entry does not carry exactly the journal fields")
    from core.wallet.nft_account import canonical
    if canonical(entry) != bytes(raw):
        raise JournalLogError("the entry is not in canonical form")
    return entry


def _pad_topic(address: str) -> str:
    return "0x" + "0" * 24 + str(address).lower().removeprefix("0x")


def read_entries(rpc: Rpc, journal_log: str, account: str, since_block: int, *,
                 to_block: Optional[int] = None, step: int = 50_000) -> List[Dict[str, Any]]:
    """Every ``Entry`` of *account* on *journal_log*, in chain order, parsed. An undecodable entry
    is skipped (anyone's account may write anything — it is data, not trusted); a failed read
    RAISES (an unreadable log is never "no entries")."""
    head = int(rpc("eth_blockNumber", []), 16) if to_block is None else int(to_block)
    logs: List[dict] = []
    start = int(since_block)
    while start <= head:
        end = min(start + step - 1, head)
        got = rpc("eth_getLogs", [{"address": journal_log, "fromBlock": hex(start),
                                   "toBlock": hex(end),
                                   "topics": [TOPIC_ENTRY, _pad_topic(account)]}])
        if not isinstance(got, list):
            raise JournalLogError(f"eth_getLogs {start}-{end} returned no list")
        logs.extend(got)
        start = end + 1
    logs.sort(key=lambda lg: (int(str(lg.get("blockNumber") or "0x0"), 16),
                              int(str(lg.get("logIndex") or "0x0"), 16)))
    out = []
    for lg in logs:
        if str(lg.get("address") or "").lower() != str(journal_log).lower():
            continue
        try:
            out.append(parse_entry(_decode_bytes(bytes.fromhex(str(lg.get("data") or "0x")[2:]))))
        except (JournalLogError, ValueError):
            continue
    return out
