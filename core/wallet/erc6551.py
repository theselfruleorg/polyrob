"""ERC-6551 token-bound accounts — an agent NFT's account, PINNED by bytecode (proposal 050
§7.2; generic since 069).

A supported account is a DIRECT ERC-1167 clone of the canonical Tokenbound ``AccountV3``
created by the canonical registry. Measured on 4663 (2026-09-22): the Tokenbound
guardian trusts NO implementation there, so the ``AccountProxy`` path cannot be
initialised — ``ACCOUNT_PROXY`` is recorded here and never used. A clone has no
ERC-1967 slot: its implementation is the 20 bytes at ``code[10:30]`` (the
ERC-1167 footer), and that is what :func:`read_implementation` reads.

⚠️ The CREATE2 salt is the RAW salt. Hashing the ``(salt, chainId, contract,
tokenId)`` tuple yields a plausible, WRONG address (``0x59c5d0bb…4873`` for the
fixture below instead of ``0xe0f47c08…6ec4``).

⚠️ Same discipline as ``tools/launchpad/pons.py``: an address is a claim, the code
hash is the check. :func:`verify` re-reads ``eth_getCode`` on every call and
refuses on mismatch.

⚠️ No signing helper lives here, by design: the NFT's owner is a valid ERC-1271 signer
of the account and the lock is not consulted for signatures, so an unscoped signing
helper would be a way to sign FOR the account.

069 v4 (the simple model): this instance acts through an account ONLY as the NFT's
current owner. There is no grant helper here (no ``setPermissions`` encoder, no
``permissions`` read): ``setPermissions`` is an account-admin selector the guard refuses.

Every read takes ``rpc`` = ``callable(method, params) -> result`` (the pons
convention), so tests pass a fake and nothing here owns a transport.
"""
from __future__ import annotations

import functools
import hashlib
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from core.wallet import abi

Rpc = Callable[[str, list], object]

#: The canonical registry — same address and code on Ethereum, Base, 4663, 46630.
REGISTRY = "0x000000006551c19487814612e58FE06813775758"
#: Tokenbound AccountV3 (``AccountV3Upgradable`` in the Tokenbound source tree).
ACCOUNT_V3_IMPL = "0x41C8f39463A868d3A88af00cd0fe7102F30E44eC"
#: Recorded, NOT used: unusable on 4663 (the guardian trusts no implementation).
ACCOUNT_PROXY = "0x55266d75D1a14E4572138116aF39863Ed6596E7F"
#: The salt every supported account uses (the RAW value, never a hash).
ACCOUNT_SALT = 0

#: sha256 of the runtime code, measured on 4663 (equal on Ethereum and Base) and,
#: for 46630, after the canonical Tokenbound calldata replay
#: (the Tokenbound deployment calldata replayed on the testnet).
CODE_SHA256: Dict[str, str] = {
    REGISTRY.lower(): "d7df998352f46d061e9e27c6a17d5108d7439482cb136c45e0f0733c7bd3da56",
    ACCOUNT_V3_IMPL.lower(): "0b890bf41c26142829c2ec32796e03fae54b1ee4c1a36477c63264277c61c160",
}

_CLONE_PREFIX = bytes.fromhex("3d60ad80600a3d3981f3363d3d373d3d3d363d73")
_CLONE_SUFFIX = bytes.fromhex("5af43d82803e903d91602b57fd5bf3")
#: runtime = 10-byte head ‖ impl ‖ 15-byte tail ‖ 128-byte footer (salt, chainId, contract, tokenId)
_RUNTIME_HEAD = bytes.fromhex("363d3d373d3d3d363d73")
_RUNTIME_LEN = 10 + 20 + 15 + 128

# Account-admin selectors (050 §7.3 rule 4). `execute` with operation != 0 is refused separately.
# C14 — the agent NEVER locks an account: `lock` is in this denylist, so no guarded call (direct,
# through `execute`, nested or batched) can set one. A sale on a marketplace needs the collection's
# lock (polyrob-desk SPEC I19: a non-owner transfer requires locked-now), so the agent cannot sell
# its NFT on a marketplace; it moves only by an owner transfer (`agent_nft_withdraw_token`, always
# owner-approved). An owner-approved `lock` verb is added only if the owner asks for one.
ADMIN_SELECTORS: Dict[str, str] = {
    abi.selector("lock(uint256)"): "lock",
    abi.selector("setPermissions(address[],bool[])"): "setPermissions",
    abi.selector("setOverrides(bytes4[],address[])"): "setOverrides",
    abi.selector("upgradeToAndCall(address,bytes)"): "upgradeToAndCall",
    # Tokenbound NestedAccountExecutor: the proof is ERC6551AccountInfo{bytes32 salt, address
    # tokenContract, uint256 tokenId}[] -> 0x1fb1ecf0 (present in the AccountV3 bytecode on 4663).
    # An empty proof makes it an `execute` with any operation, so the scan must see it.
    abi.selector("executeNested(address,uint256,bytes,uint8,(bytes32,address,uint256)[])"): "executeNested",
    abi.selector("executeBatch((address,uint256,bytes,uint8)[])"): "executeBatch",
}
EXECUTE_SELECTOR = abi.selector("execute(address,uint256,bytes,uint8)")
#: Every selector that drives an account: ``execute`` (any operation) plus the admin verbs.
ACCOUNT_CALL_SELECTORS: Dict[str, str] = {EXECUTE_SELECTOR: "execute", **ADMIN_SELECTORS}

#: ERC-2771 forwarders AccountV3 trusts (``isTrustedForwarder(f) == true``). A call THROUGH
#: one reaches the account with ``_msgSender()`` = the ORIGINAL sender, so the owner key
#: that sends ``forwarder.aggregate3([(account, execute(...))])`` drives the account while
#: the transaction's own ``to`` is the forwarder. MEASURED on 4663 (G0.2, 2026-09-29): the
#: guard read such a call as a 1-wei treasury send and authorized it; 0.05 ETH left the
#: account on replay. Static pin here; the ``via_account`` pre-flight also reads
#: ``isTrustedForwarder`` on the account for the inner destination (core handoff W1).
TRUSTED_FORWARDERS: Dict[str, str] = {
    "0xca1167915584462449ee5b4ea51c37fe81ecdccd": "Tokenbound ERC-2771 forwarder (Multicall3-shaped)",
}

# The pinned collections (address, supply cap, account versions) are the owner-written
# profiles in ``core/wallet/collection_registry.py`` (069 v4 §4), EMPTY by default. The
# guard's no-nesting rule does not wait for a pin: a token may never enter an account of its
# OWN collection, whatever the collection (``tx_guard._nft_nesting_refusal``).

TOPIC_APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
TOPIC_APPROVAL_FOR_ALL = "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31"

#: Uniswap Permit2 — same address on every chain (canonical CREATE2). A Permit2 allowance is held
#: BY Permit2, keyed ``(owner, token, spender)``, and SURVIVES a sale like any approval.
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
#: ``Approval(address indexed owner, address indexed token, address indexed spender, uint160 amount, uint48 expiration)``
TOPIC_PERMIT2_APPROVAL = "0xda9fa7c1b00402c17d0161b249b1ab8bbec047c5a52207b9c112deffd817036b"
#: ``Permit(address indexed owner, address indexed token, address indexed spender, uint160, uint48, uint48 nonce)``
TOPIC_PERMIT2_PERMIT = "0xc6a377bfc4eb120024a8ac08eef205be16b817020812c73223e81d1bdb9708ec"
#: ``Lockdown(address indexed owner, address token, address spender)`` — sets the amount to 0.
TOPIC_PERMIT2_LOCKDOWN = "0x89b1add15eff56b3dfe299ad94e01f2b52fbcb80ae1a3baea6ae8c04cb2b98a4"

#: ERC-6909 (multi-token; Uniswap v4 claims) — ``Approval(address indexed owner, address indexed
#: spender, uint256 indexed id, uint256 amount)`` and ``OperatorSet(address indexed owner, address
#: indexed spender, bool approved)``. Both survive a sale like any approval (C17).
TOPIC_6909_APPROVAL = "0xb3fd5071835887567a0671151121894ddccc2842f1d10bedad13e0d17cace9a7"
TOPIC_6909_OPERATOR_SET = "0xceb576d9f15e4e200fdb5096d64d5dfd667e16def20c1eefd14256d8e3faa267"

#: The approval kinds :func:`open_approvals` scans. The agent-NFT package reads this to decide whether
#: the table it shows is COMPLETE ("permit2" present) or must say "incomplete".
APPROVAL_KINDS: Tuple[str, ...] = ("erc20", "erc721", "operator", "permit2", "erc6909",
                                   "erc6909_operator")


class Erc6551Error(RuntimeError):
    """A pin, a read or a shape the account depends on is not what it must be. Always says which."""


# ==========================================================================
# Pure math + encoders
# ==========================================================================

def _addr20(value: str) -> bytes:
    raw = bytes.fromhex(str(value).lower().removeprefix("0x"))
    if len(raw) != 20:
        raise Erc6551Error(f"not a 20-byte address: {value!r}")
    return raw


def _checksum(raw: bytes) -> str:
    from eth_utils import to_checksum_address
    return to_checksum_address("0x" + raw.hex())


def account_address(chain_id: int, token_contract: str, token_id: int, salt: int = ACCOUNT_SALT,
                    implementation: str = ACCOUNT_V3_IMPL) -> str:
    """The registry's ``account(impl, salt, chainId, contract, tokenId)`` computed offline (pure CREATE2)."""
    from eth_utils import keccak
    salt_b = int(salt).to_bytes(32, "big")
    footer = salt_b + int(chain_id).to_bytes(32, "big") + (b"\x00" * 12 + _addr20(token_contract)) \
        + int(token_id).to_bytes(32, "big")
    creation = _CLONE_PREFIX + _addr20(implementation) + _CLONE_SUFFIX + footer
    digest = keccak(b"\xff" + _addr20(REGISTRY) + salt_b + keccak(creation))
    return _checksum(digest[12:])


@functools.lru_cache(maxsize=8)
def _collection_account_set(chain_id: int, collection_lower: str, max_supply: int,
                            implementation_lower: str, salt: int) -> frozenset:
    from eth_utils import keccak
    salt_b = int(salt).to_bytes(32, "big")
    head = _CLONE_PREFIX + _addr20(implementation_lower) + _CLONE_SUFFIX + salt_b \
        + int(chain_id).to_bytes(32, "big") + b"\x00" * 12 + _addr20(collection_lower)
    pre = b"\xff" + _addr20(REGISTRY) + salt_b
    return frozenset("0x" + keccak(pre + keccak(head + int(k).to_bytes(32, "big")))[12:].hex()
                     for k in range(0, max_supply + 1))


def collection_account_addresses(chain_id: int, collection: str, max_supply: int, *,
                                 implementation: str = ACCOUNT_V3_IMPL,
                                 salt: int = ACCOUNT_SALT) -> frozenset:
    """The lowercase ERC-6551 account address of EVERY token id ``0..max_supply`` of *collection*
    (the canonical registry; AccountV3 and salt 0 unless a profile's account version says
    otherwise) — pure CREATE2, cached per collection and chain. An account that is not
    deployed yet has no code, so a code read cannot see it; this set can. ``max_supply`` is
    the collection profile's cap (``core/wallet/collection_registry.py``)."""
    return _collection_account_set(int(chain_id), str(collection).lower(), int(max_supply),
                                   str(implementation).lower(), int(salt))


@functools.lru_cache(maxsize=8)
def _collection_account_index(chain_id: int, collection_lower: str, max_supply: int,
                              implementation_lower: str, salt: int) -> Dict[str, int]:
    return {account_address(chain_id, collection_lower, k, salt=salt,
                            implementation=implementation_lower).lower(): k
            for k in range(0, max_supply + 1)}


def collection_account_token_id(chain_id: int, collection: str, max_supply: int, address: str, *,
                                implementation: str = ACCOUNT_V3_IMPL,
                                salt: int = ACCOUNT_SALT) -> Optional[int]:
    """The token id ``k`` whose account (this registry/implementation/salt) is *address*, or None.
    The inverse of :func:`collection_account_addresses`; built on the first hit, then cached."""
    a = str(address or "").lower()
    if a not in collection_account_addresses(chain_id, collection, max_supply,
                                             implementation=implementation, salt=salt):
        return None
    return _collection_account_index(int(chain_id), str(collection).lower(), int(max_supply),
                                     str(implementation).lower(), int(salt)).get(a)


def encode_create_account(chain_id: int, token_contract: str, token_id: int,
                          salt: int = ACCOUNT_SALT) -> str:
    return abi.encode_call(
        "createAccount",
        [{"type": "address"}, {"type": "bytes32"}, {"type": "uint256"}, {"type": "address"},
         {"type": "uint256"}],
        [ACCOUNT_V3_IMPL, "0x" + int(salt).to_bytes(32, "big").hex(), int(chain_id), token_contract,
         int(token_id)])


def encode_execute(to: str, value: int, data: str, op: int = 0) -> str:
    """``execute(to, value, data, operation)``. The guard refuses ``op != 0``."""
    return abi.encode_call(
        "execute", [{"type": "address"}, {"type": "uint256"}, {"type": "bytes"}, {"type": "uint8"}],
        [to, int(value), data or "0x", int(op)])


def decode_execute(data: str) -> Tuple[str, int, str, int]:
    """Inverse of :func:`encode_execute`; raises on any other selector."""
    if not isinstance(data, str) or data[:10].lower() != EXECUTE_SELECTOR:
        raise Erc6551Error("not an execute(address,uint256,bytes,uint8) call")
    to, value, inner, op = abi.decode(
        [{"type": "address"}, {"type": "uint256"}, {"type": "bytes"}, {"type": "uint8"}], data[10:])
    inner_hex = inner if isinstance(inner, str) else "0x" + bytes(inner).hex()
    return to, int(value), inner_hex, int(op)


EXECUTE_BATCH_SELECTOR = abi.selector("executeBatch((address,uint256,bytes,uint8)[])")


def encode_execute_batch(ops: Iterable[Tuple[str, int, str, int]]) -> str:
    """``executeBatch((to, value, data, operation)[])``. The guard accepts it ONLY as the declared
    approve-spend-reset shape (``TxIntent.via_account_batch``)."""
    from eth_abi import encode
    rows = [(to, int(v), bytes.fromhex(str(d or "0x")[2:]), int(op)) for (to, v, d, op) in ops]
    return EXECUTE_BATCH_SELECTOR + encode(["(address,uint256,bytes,uint8)[]"], [rows]).hex()


def decode_execute_batch(data: str) -> List[Tuple[str, int, str, int]]:
    """Inverse of :func:`encode_execute_batch`; raises on any other selector or a malformed body."""
    if not isinstance(data, str) or data[:10].lower() != EXECUTE_BATCH_SELECTOR:
        raise Erc6551Error("not an executeBatch((address,uint256,bytes,uint8)[]) call")
    from eth_abi import decode
    (rows,) = decode(["(address,uint256,bytes,uint8)[]"], bytes.fromhex(data[10:]))
    return [(_checksum(_addr20(to)), int(v), "0x" + bytes(d).hex(), int(op)) for (to, v, d, op) in rows]


def encode_lock(locked_until: int) -> str:
    """``lock(lockedUntil)`` calldata — for tests and reads of what a lock looks like. No verb
    sends it: ``lock`` is an account-admin selector the guard refuses (C14)."""
    return abi.encode_call("lock", [{"type": "uint256"}], [int(locked_until)])


def admin_selector_name(data: Optional[str]) -> Optional[str]:
    """The account-admin verb a calldata invokes, or ``None``. ``execute`` with ``op != 0``
    counts as admin (a delegatecall/create runs arbitrary code AS the account)."""
    if not isinstance(data, str) or len(data) < 10:
        return None
    sel = data[:10].lower()
    if sel in ADMIN_SELECTORS:
        return ADMIN_SELECTORS[sel]
    if sel == EXECUTE_SELECTOR:
        try:
            _, _, _, op = decode_execute(data)
        except Exception:  # noqa: BLE001 — malformed execute is refused as admin
            return "execute(malformed)"
        if op != 0:
            return f"execute(operation={op})"
    return None


def embedded_account_selector(data: Optional[str]) -> Optional[str]:
    """The account-call verb (``execute`` or an admin verb) whose selector appears ANYWHERE in
    *data*, at any byte offset, or ``None``.

    A top-level check is not enough: a multicall / forwarder / router carries the account call
    as an ABI ``bytes`` argument (G0.2: ``aggregate3([(account, false, execute(...))])``), and a
    packed encoding may put it at any offset. Byte-aligned scan, every depth at once. A false
    positive needs one of seven 4-byte values to occur by chance in the calldata (≈ 1.6e-9 per
    byte offset) — the price of never missing a nested account call.
    """
    if not isinstance(data, str):
        return None
    body = data.lower().removeprefix("0x")
    for sel, name in ACCOUNT_CALL_SELECTORS.items():
        needle = sel[2:]
        i = body.find(needle)
        while i != -1:
            if i % 2 == 0:
                return name
            i = body.find(needle, i + 1)
    return None


def is_trusted_forwarder(address: Optional[str]) -> bool:
    """True when *address* is a PINNED ERC-2771 forwarder AccountV3 trusts (static list)."""
    return bool(address) and str(address).lower() in TRUSTED_FORWARDERS


# ==========================================================================
# Reads (rpc = callable(method, params))
# ==========================================================================

def _code(rpc: Rpc, address: str) -> bytes:
    code = rpc("eth_getCode", [address, "latest"])
    if not isinstance(code, str) or len(code) <= 2:
        return b""
    return bytes.fromhex(code[2:])


def verify(rpc: Rpc) -> None:
    """The registry and AccountV3 must be EXACTLY the reviewed code. Re-read per call; raises on mismatch."""
    for address, expected in CODE_SHA256.items():
        code = _code(rpc, address)
        if not code:
            raise Erc6551Error(f"{address} has NO code on this chain — the pinned ERC-6551 "
                               f"deployment is not there. Refusing.")
        got = hashlib.sha256(code).hexdigest()
        if got != expected:
            raise Erc6551Error(f"the code at {address} hashes to {got[:8]}…, not the pinned "
                               f"{expected[:8]}…. Refusing to act through un-reviewed code.")


def read_implementation(rpc: Rpc, account: str) -> str:
    """The implementation an account clones: ``code[10:30]`` (ERC-1167 footer) — NOT an ERC-1967 slot."""
    code = _code(rpc, account)
    if len(code) != _RUNTIME_LEN or code[:10] != _RUNTIME_HEAD:
        raise Erc6551Error(f"{account} is not an ERC-6551 clone (code length {len(code)})")
    return _checksum(code[10:30])


def read_clone(rpc: Rpc, address: str) -> Optional[Tuple[str, int, int, str, int]]:
    """``(implementation, salt, chainId, tokenContract, tokenId)`` when *address* is an ERC-6551
    registry clone (the 173-byte ERC-1167 runtime + footer, ANY implementation), else ``None``.
    An RPC failure RAISES — "could not read" is never "not an account"."""
    code = _code(rpc, address)
    if len(code) != _RUNTIME_LEN or code[:10] != _RUNTIME_HEAD or code[30:45] != _CLONE_SUFFIX:
        return None
    f = code[45:]
    return (_checksum(code[10:30]), int.from_bytes(f[0:32], "big"), int.from_bytes(f[32:64], "big"),
            _checksum(f[76:96]), int.from_bytes(f[96:128], "big"))


def read_footer(rpc: Rpc, account: str) -> Tuple[int, int, str, int]:
    """``(salt, chainId, tokenContract, tokenId)`` from the clone's 128-byte footer."""
    code = _code(rpc, account)
    if len(code) != _RUNTIME_LEN:
        raise Erc6551Error(f"{account} is not an ERC-6551 clone (code length {len(code)})")
    f = code[45:]
    return (int.from_bytes(f[0:32], "big"), int.from_bytes(f[32:64], "big"), _checksum(f[76:96]),
            int.from_bytes(f[96:128], "big"))


def _call(rpc: Rpc, to: str, name: str, inputs: list, values: list, outputs: list):
    raw = rpc("eth_call", [{"to": to, "data": abi.encode_call(name, inputs, values)}, "latest"])
    if raw in (None, "0x", ""):
        raise Erc6551Error(f"{name}() on {to} returned nothing — refusing to guess")
    out = abi.decode(outputs, raw)
    return out[0] if len(out) == 1 else out


def read_owner(rpc: Rpc, account: str) -> str:
    return _call(rpc, account, "owner", [], [], [{"type": "address"}])


def read_state(rpc: Rpc, account: str) -> int:
    return int(_call(rpc, account, "state", [], [], [{"type": "uint256"}]))


def read_locked_until(rpc: Rpc, account: str) -> int:
    return int(_call(rpc, account, "lockedUntil", [], [], [{"type": "uint256"}]))


def read_is_locked(rpc: Rpc, account: str) -> bool:
    return bool(_call(rpc, account, "isLocked", [], [], [{"type": "bool"}]))


#: ERC-6551 ``isValidSigner`` and ERC-1271 ``isValidSignature`` magic values.
IS_VALID_SIGNER_MAGIC = "0x523e3260"
IS_VALID_SIGNATURE_MAGIC = "0x1626ba7e"


def read_token(rpc: Rpc, account: str) -> Tuple[int, str, int]:
    """ERC-6551 ``token()`` -> ``(chainId, tokenContract, tokenId)`` — the account's own answer
    (the footer read offline by :func:`read_footer` must agree with it)."""
    chain_id, contract, token_id = _call(rpc, account, "token", [], [],
                                         [{"type": "uint256"}, {"type": "address"}, {"type": "uint256"}])
    return int(chain_id), _checksum(_addr20(contract)), int(token_id)


def _magic(raw) -> str:
    return "0x" + (raw.hex() if isinstance(raw, (bytes, bytearray)) else str(raw).lower()
                   .removeprefix("0x"))[:8]


def read_is_valid_signer(rpc: Rpc, account: str, signer: str, context: str = "0x") -> bool:
    """ERC-6551 ``isValidSigner(signer, context)`` == magic. AccountV3: the owner or a permissioned
    caller. Raises when unreadable."""
    raw = _call(rpc, account, "isValidSigner", [{"type": "address"}, {"type": "bytes"}],
                [signer, context or "0x"], [{"type": "bytes4"}])
    return _magic(raw) == IS_VALID_SIGNER_MAGIC


def read_is_valid_signature(rpc: Rpc, account: str, digest: str, signature: str) -> bool:
    """ERC-1271 ``isValidSignature(hash, signature)`` == magic. ⚠️ AccountV3 checks raw ECDSA with
    no account in the digest and does NOT consult the lock. Raises when unreadable; a revert
    (invalid signature) raises too — callers treat "not proven valid" as False."""
    raw = _call(rpc, account, "isValidSignature", [{"type": "bytes32"}, {"type": "bytes"}],
                [digest, signature], [{"type": "bytes4"}])
    return _magic(raw) == IS_VALID_SIGNATURE_MAGIC


def read_is_trusted_forwarder(rpc: Rpc, account: str, forwarder: str) -> bool:
    """``isTrustedForwarder(forwarder)`` on the account (ERC-2771). Raises when unreadable."""
    return bool(_call(rpc, account, "isTrustedForwarder", [{"type": "address"}], [forwarder],
                      [{"type": "bool"}]))


# ==========================================================================
# Open approvals — what survives a sale (F1)
# ==========================================================================

@dataclass(frozen=True)
class OpenApproval:
    #: "erc20" | "erc721" | "operator" (ApprovalForAll: 721 or 1155) | "permit2" | "erc6909"
    #: (ERC-6909 per-id allowance) | "erc6909_operator" (ERC-6909 OperatorSet)
    kind: str
    contract: str             # the token (for "permit2": the ERC-20 whose Permit2 allowance this is)
    spender: str              # spender / approved / operator
    token_id: Optional[int]   # erc721 / erc6909
    amount: Optional[int]     # erc20 / permit2 / erc6909 (live allowance when verified)
    block: int
    verified: bool            # True = confirmed by a live read; False = the live read failed (kept: fail closed)
    expiration: Optional[int] = None  # permit2 only (unix seconds)
    #: The transactions whose logs opened / kept this row open (since its last reset).
    tx_hashes: Tuple[str, ...] = ()
    #: True = one of those transactions was SENT by an owner of the NFT (the only party that
    #: can make the account approve); False = none was (any contract can emit an ``Approval``
    #: naming the account — the row may be fabricated); None = not judged / unreadable.
    attributed: Optional[bool] = None

    @property
    def key(self) -> str:
        """The stable id an owner names to accept an unattributed row (``approval_key``)."""
        return approval_key(self)


def approval_key(r: "OpenApproval") -> str:
    tid = "" if r.token_id is None else str(int(r.token_id))
    spender = "" if r.kind == "erc721" else str(r.spender).lower()
    return f"{r.kind}:{str(r.contract).lower()}:{spender}:{tid}"


def _topic_addr(topic: str) -> str:
    return _checksum(bytes.fromhex(topic[-40:]))


def _pad_topic(address: str) -> str:
    return "0x" + "00" * 12 + _addr20(address).hex()


def _scan_logs(rpc: Rpc, account: str, since_block: int, to_block: int, step: int) -> List[dict]:
    logs: List[dict] = []
    start = int(since_block)
    while start <= to_block:
        end = min(start + step - 1, to_block)
        got = rpc("eth_getLogs", [{
            "fromBlock": hex(start), "toBlock": hex(end),
            "topics": [[TOPIC_APPROVAL, TOPIC_APPROVAL_FOR_ALL, TOPIC_PERMIT2_APPROVAL,
                        TOPIC_PERMIT2_PERMIT, TOPIC_PERMIT2_LOCKDOWN, TOPIC_6909_APPROVAL,
                        TOPIC_6909_OPERATOR_SET], _pad_topic(account)],
        }])
        if not isinstance(got, list):
            raise Erc6551Error(f"eth_getLogs {start}-{end} returned no list — refusing to report 'none'")
        logs.extend(got)
        start = end + 1
    return logs


def open_approvals(rpc: Rpc, account: str, since_block: int, *, to_block: Optional[int] = None,
                   step: int = 50_000, live: bool = True) -> List[OpenApproval]:
    """Every approval the account has granted and not revoked (kinds: :data:`APPROVAL_KINDS`).

    Log scan: ``Approval``/``ApprovalForAll`` with ``topics[1] == account``, and Permit2's
    ``Approval``/``Permit``/``Lockdown`` emitted BY the pinned :data:`PERMIT2` with the account as
    owner; the LAST event per ``(contract, spender)`` (ERC-20), ``(contract, tokenId)`` (ERC-721),
    ``(contract, operator)`` (ApprovalForAll) or ``(token, spender)`` (Permit2) wins; zero / false
    rows drop. With ``live`` each candidate is then confirmed by a view call, because OZ 5 lowers an
    allowance in ``transferFrom`` and clears an ERC-721 approval on transfer WITHOUT an event, and
    Permit2 lowers its amount on a transfer without one too (Permit2: ``allowance(owner, token,
    spender)``, open while ``amount > 0`` and not expired). A live read that fails keeps the row
    (``verified=False``): a missed approval is the dangerous error, an extra row is not.
    """
    if to_block is None:
        to_block = int(rpc("eth_blockNumber", []), 16)
    last: Dict[tuple, OpenApproval] = {}
    hashes: Dict[tuple, list] = {}
    log_hash = [None]

    def _set(key, row):
        last[key] = row
        if row is None:
            hashes[key] = []
        elif log_hash[0]:
            hashes.setdefault(key, []).append(log_hash[0])
    permit2 = PERMIT2.lower()
    for log in sorted(_scan_logs(rpc, account, since_block, to_block, step),
                      key=lambda x: (int(x["blockNumber"], 16), int(x.get("logIndex", "0x0"), 16))):
        topics = [t.lower() for t in log.get("topics") or []]
        contract = _checksum(_addr20(log["address"]))
        block = int(log["blockNumber"], 16)
        data = (log.get("data") or "0x")[2:]
        log_hash[0] = str(log.get("transactionHash") or "").lower() or None
        if topics[0] in (TOPIC_PERMIT2_APPROVAL, TOPIC_PERMIT2_PERMIT, TOPIC_PERMIT2_LOCKDOWN):
            if contract.lower() != permit2:
                continue  # the same topic from any other contract is not a Permit2 allowance
            if topics[0] == TOPIC_PERMIT2_LOCKDOWN and len(topics) == 2:
                token, spender = _topic_addr(data[0:64]), _topic_addr(data[64:128])
                _set(("permit2", token.lower(), spender.lower()), None)
            elif len(topics) == 4:
                token, spender = _topic_addr(topics[2]), _topic_addr(topics[3])
                amount = int(data[0:64] or "0", 16)
                expiration = int(data[64:128] or "0", 16)
                _set(("permit2", token.lower(), spender.lower()), OpenApproval(
                    "permit2", token, spender, None, amount, block, False, expiration) if amount else None)
            continue
        if topics[0] == TOPIC_6909_OPERATOR_SET and len(topics) == 3:
            op = _topic_addr(topics[2])
            approved = int(data[:64] or "0", 16) != 0
            _set(("erc6909_operator", contract.lower(), op.lower()), OpenApproval(
                "erc6909_operator", contract, op, None, None, block, False) if approved else None)
        elif topics[0] == TOPIC_6909_APPROVAL and len(topics) == 4:
            spender = _topic_addr(topics[2])
            tid = int(topics[3], 16)
            amount = int(data[:64] or "0", 16)
            _set(("erc6909", contract.lower(), spender.lower(), tid), OpenApproval(
                "erc6909", contract, spender, tid, amount, block, False) if amount else None)
        elif topics[0] == TOPIC_APPROVAL_FOR_ALL and len(topics) == 3:
            op = _topic_addr(topics[2])
            approved = int(data[:64] or "0", 16) != 0
            _set(("operator", contract.lower(), op.lower()), OpenApproval(
                "operator", contract, op, None, None, block, False) if approved else None)
        elif topics[0] == TOPIC_APPROVAL and len(topics) == 4:     # ERC-721: tokenId indexed
            spender = _topic_addr(topics[2])
            tid = int(topics[3], 16)
            is_zero = int(topics[2], 16) == 0
            _set(("erc721", contract.lower(), tid), None if is_zero else OpenApproval(
                "erc721", contract, spender, tid, None, block, False))
        elif topics[0] == TOPIC_APPROVAL and len(topics) == 3:     # ERC-20: value in data
            spender = _topic_addr(topics[2])
            amount = int(data[:64] or "0", 16)
            _set(("erc20", contract.lower(), spender.lower()), OpenApproval(
                "erc20", contract, spender, None, amount, block, False) if amount else None)
    import dataclasses as _dc
    rows = [_dc.replace(r, tx_hashes=tuple(hashes.get(k, ())))
            for k, r in last.items() if r is not None]
    if not live:
        return rows
    out: List[OpenApproval] = []
    now: Optional[int] = None
    if any(r.kind == "permit2" for r in rows):
        try:
            now = int(rpc("eth_getBlockByNumber", ["latest", False])["timestamp"], 16)
        except Exception:  # noqa: BLE001 — no clock: every Permit2 row stays, unverified
            now = None
    for r in rows:
        try:
            if r.kind == "permit2" and now is None:
                raise Erc6551Error("no block timestamp to judge a Permit2 expiration against")
            still = _still_open(rpc, account, r, now=now)
        except Exception:  # noqa: BLE001 — fail closed: keep the row, unverified
            out.append(r)
            continue
        if still is not None:
            out.append(_dc.replace(still, tx_hashes=r.tx_hashes))
    return out


TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def nft_owners(rpc: Rpc, collection: str, token_id: int, since_block: int, to_block: int, *,
               step: int = 50_000) -> set:
    """Every address that has held ``collection #token_id`` (``Transfer`` ``to``), from the
    collection's deploy block. These — and only these — could make its account approve."""
    owners = set()
    start = int(since_block)
    tid = "0x" + f"{int(token_id):064x}"
    while start <= to_block:
        end = min(start + step - 1, to_block)
        got = rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end), "address": collection,
                                   "topics": [TOPIC_TRANSFER, None, None, tid]}])
        if not isinstance(got, list):
            raise Erc6551Error(f"eth_getLogs {start}-{end} returned no list — the owner history is unread")
        for log in got:
            topics = [t.lower() for t in log.get("topics") or []]
            if (str(log.get("address") or "").lower() == str(collection).lower() and len(topics) == 4
                    and topics[0] == TOPIC_TRANSFER and topics[3] == tid):
                owners.add(_topic_addr(topics[2]).lower())
        start = end + 1
    return owners


def attribute(rpc: Rpc, rows: List[OpenApproval], owners: Iterable[str]) -> List[OpenApproval]:
    """Mark each row ``attributed``: True when a transaction an NFT owner SENT emitted one of
    its events, False when every such transaction was sent by someone else, None when a
    transaction could not be read. Nothing is dropped: the caller decides what to do with an
    unattributed row (a real approval may also come from a signature someone else submitted)."""
    import dataclasses as _dc
    owners = {str(o).lower() for o in owners}
    out = []
    for r in rows:
        verdict: Optional[bool] = False if r.tx_hashes else None
        for h in r.tx_hashes:
            try:
                tx = rpc("eth_getTransactionByHash", [h])
                if not isinstance(tx, dict) or str(tx.get("hash") or "").lower() != h.lower():
                    raise Erc6551Error("transaction identity mismatch")
            except Exception:  # noqa: BLE001 — unreadable: undecided, never "attributed"
                verdict = None
                continue
            if str(tx.get("from") or "").lower() in owners:
                verdict = True
                break
        out.append(_dc.replace(r, attributed=verdict))
    return out


def read_permit2_allowance(rpc: Rpc, owner: str, token: str, spender: str) -> Tuple[int, int, int]:
    """Permit2 ``allowance(owner, token, spender)`` -> ``(amount, expiration, nonce)``."""
    out = _call(rpc, PERMIT2, "allowance", [{"type": "address"}] * 3, [owner, token, spender],
                [{"type": "uint160"}, {"type": "uint48"}, {"type": "uint48"}])
    return int(out[0]), int(out[1]), int(out[2])


def encode_permit2_revoke(token: str, spender: str) -> str:
    """Permit2 ``approve(token, spender, 0, 0)`` — the revoke shape the guard asserts
    (``TxIntent.permit2_revokes``): Permit2 emits ``Approval(owner, token, spender, 0, …)``."""
    return abi.encode_call("approve", [{"type": "address"}, {"type": "address"}, {"type": "uint160"},
                                       {"type": "uint48"}], [token, spender, 0, 0])


def encode_erc721_revoke(token_id: int) -> str:
    """ERC-721 ``approve(address(0), tokenId)`` — clears ONE token's approval
    (``TxIntent.nft_approval_revokes``)."""
    return abi.encode_call("approve", [{"type": "address"}, {"type": "uint256"}],
                           ["0x" + "00" * 20, int(token_id)])


def encode_erc6909_revoke(spender: str, token_id: int) -> str:
    """ERC-6909 ``approve(spender, id, 0)`` (``TxIntent.erc6909_revokes``)."""
    return abi.encode_call("approve", [{"type": "address"}, {"type": "uint256"}, {"type": "uint256"}],
                           [spender, int(token_id), 0])


def encode_erc6909_operator_revoke(spender: str) -> str:
    """ERC-6909 ``setOperator(spender, false)`` (``TxIntent.nft_operator_ops`` with False)."""
    return abi.encode_call("setOperator", [{"type": "address"}, {"type": "bool"}], [spender, False])


def _still_open(rpc: Rpc, account: str, r: OpenApproval, *, now: Optional[int] = None) -> Optional[OpenApproval]:
    if r.kind == "permit2":
        amount, expiration, _nonce = read_permit2_allowance(rpc, account, r.contract, r.spender)
        if amount == 0 or (now is not None and expiration < now):
            return None  # spent down, revoked or expired: Permit2 refuses a transfer past expiration
        return OpenApproval(r.kind, r.contract, r.spender, None, amount, r.block, True, expiration)
    if r.kind == "erc20":
        amount = int(_call(rpc, r.contract, "allowance", [{"type": "address"}, {"type": "address"}],
                           [account, r.spender], [{"type": "uint256"}]))
        return OpenApproval(r.kind, r.contract, r.spender, None, amount, r.block, True) if amount else None
    if r.kind == "erc6909":
        amount = int(_call(rpc, r.contract, "allowance",
                           [{"type": "address"}, {"type": "address"}, {"type": "uint256"}],
                           [account, r.spender, int(r.token_id)], [{"type": "uint256"}]))
        return OpenApproval(r.kind, r.contract, r.spender, r.token_id, amount, r.block, True) if amount else None
    if r.kind == "erc6909_operator":
        ok = bool(_call(rpc, r.contract, "isOperator", [{"type": "address"}, {"type": "address"}],
                        [account, r.spender], [{"type": "bool"}]))
        return OpenApproval(r.kind, r.contract, r.spender, None, None, r.block, True) if ok else None
    if r.kind == "operator":
        ok = bool(_call(rpc, r.contract, "isApprovedForAll", [{"type": "address"}, {"type": "address"}],
                        [account, r.spender], [{"type": "bool"}]))
        return OpenApproval(r.kind, r.contract, r.spender, None, None, r.block, True) if ok else None
    owner = _call(rpc, r.contract, "ownerOf", [{"type": "uint256"}], [r.token_id], [{"type": "address"}])
    if str(owner).lower() != account.lower():
        return None  # the token left the account; its approval went with it
    approved = _call(rpc, r.contract, "getApproved", [{"type": "uint256"}], [r.token_id],
                     [{"type": "address"}])
    if int(str(approved), 16) == 0:
        return None
    return OpenApproval(r.kind, r.contract, _checksum(_addr20(approved)), r.token_id, None, r.block, True)
