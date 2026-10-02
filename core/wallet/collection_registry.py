"""Owner-pinned collection profiles — the ONE place a collection becomes "pinned" (069 v4 §4).

A profile is the manifest of one agent-NFT collection on one chain. The guard reads it for
three rules and nothing else:

* the collection mint and reveal shapes (``core/wallet/collection_mint.py``,
  ``collection_reveal.py``) pay / call ONLY a pinned collection whose ``capabilities`` name
  that shape — no pinned collection means every mint and reveal refuses;
* the nesting rules in ``tx_guard`` (a pinned collection's token may not move into the
  ERC-6551 account of ANY of that collection's ids ``0..max_supply``, deployed or not);
* the journal: a profile's ``journal_prefix`` joins the default ``agent``
  (``core/wallet/account_journal.py``).

⚠️ WHERE IT LIVES. ``/etc/polyrob/agent_nft_collections.json`` — beside ``signer.toml`` and
``polyrob.env``: root writes it, the agent and the signer only read it. A pin the agent could
write would let it pay any contract as a "collection". So a file this process CAN write is
refused (unless the process is root, i.e. the owner on the box), exactly like an unreadable
or malformed one. The signer re-runs ``tx_guard`` in its own process and reads the file
itself; the agent never passes a profile over the wire.

Empty by default: no file = no pinned collection (the shipped state). A file that exists but
cannot be read, is writable by this process, or does not match the schema RAISES
:class:`CollectionRegistryError` — "I could not read the pins" is never "nothing is pinned".
An unknown ``spec``, capability, account version or call shape refuses the whole file (fail
closed): an agent must never act on a profile it does not understand.

Schema (JSON)::

    {"profiles": [{
        "spec": "agent-nft-profile/1",
        "capabilities": ["mint", "reveal"],
        "chain_id": 4663,
        "address": "0x…",                       # the collection contract
        "runtime_sha256": "…64 hex…",           # its runtime code hash (recorded)
        "deploy_block": 123,
        "max_supply": 6551,                     # token ids are 1..max_supply
        "accounts": [{"registry": "0x0000…5758", "implementation": "0x41C8…44eC", "salt": 0}],
        "journal_prefix": "POLYROB",            # optional
        "call_shapes": {"mint": "mint(address,uint256,uint256)",
                        "reveal": "reveal(uint256[])"}   # optional; must equal the known shapes
    }]}

``runtime_sha256`` is re-checked against the collection's live code
(:func:`runtime_refusal`) by ``tx_guard`` whenever it judges a collection mint, a reveal or a
move of one of the collection's tokens (069 v4 A3/§5), and by ``core.wallet.nft_account``
before the agent acts from one of its accounts: a pin whose code changed is no pin.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: The owner-written registry file (root-owned; the agent cannot write it).
REGISTRY_FILE = "/etc/polyrob/agent_nft_collections.json"

SPEC_V1 = "agent-nft-profile/1"
#: The profile specs this code understands. Anything else refuses the whole file.
KNOWN_SPECS = frozenset({SPEC_V1})
CAP_MINT = "mint"
CAP_REVEAL = "reveal"
#: The capabilities this code understands, each with the ONE call shape core implements.
KNOWN_CALL_SHAPES: Dict[str, str] = {
    CAP_MINT: "mint(address,uint256,uint256)",
    CAP_REVEAL: "reveal(uint256[])",
}

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_KEYS = frozenset({"spec", "capabilities", "chain_id", "address", "runtime_sha256",
                           "deploy_block", "max_supply", "accounts", "journal_prefix",
                           "call_shapes"})
_REQUIRED_KEYS = _PROFILE_KEYS - {"journal_prefix", "call_shapes"}
_ACCOUNT_KEYS = frozenset({"registry", "implementation", "salt"})


class CollectionRegistryError(RuntimeError):
    """The registry file exists but cannot be trusted (unreadable, writable, or off-schema)."""


@dataclass(frozen=True)
class AccountVersion:
    registry: str        # lowercase
    implementation: str  # lowercase
    salt: int


@dataclass(frozen=True)
class CollectionProfile:
    spec: str
    capabilities: Tuple[str, ...]
    chain_id: int
    address: str         # lowercase
    runtime_sha256: str
    deploy_block: int
    max_supply: int
    accounts: Tuple[AccountVersion, ...]
    journal_prefix: Optional[str] = None


def _int(value, what: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CollectionRegistryError(f"{what} must be an integer >= {minimum}, got {value!r}")
    return value


def _address(value, what: str) -> str:
    if not isinstance(value, str) or not _ADDRESS.match(value):
        raise CollectionRegistryError(f"{what} must be a 0x address, got {value!r}")
    return value.lower()


def _parse_profile(i: int, raw) -> CollectionProfile:
    from core.wallet import account_journal, erc6551
    where = f"profiles[{i}]"
    if not isinstance(raw, dict):
        raise CollectionRegistryError(f"{where} is not an object")
    unknown = set(raw) - _PROFILE_KEYS
    if unknown:
        raise CollectionRegistryError(f"{where} has unknown field(s) {sorted(unknown)}")
    missing = _REQUIRED_KEYS - set(raw)
    if missing:
        raise CollectionRegistryError(f"{where} is missing {sorted(missing)}")
    spec = raw["spec"]
    if spec not in KNOWN_SPECS:
        raise CollectionRegistryError(f"{where} spec {spec!r} is unknown (this build knows "
                                      f"{sorted(KNOWN_SPECS)}) — refusing the registry")
    caps = raw["capabilities"]
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        raise CollectionRegistryError(f"{where} capabilities must be a list of names")
    bad = [c for c in caps if c not in KNOWN_CALL_SHAPES]
    if bad:
        raise CollectionRegistryError(f"{where} capability {bad} is unknown (this build knows "
                                      f"{sorted(KNOWN_CALL_SHAPES)}) — refusing the registry")
    shapes = raw.get("call_shapes", {})
    if not isinstance(shapes, dict):
        raise CollectionRegistryError(f"{where} call_shapes must be an object")
    for cap, sig in shapes.items():
        if cap not in caps:
            raise CollectionRegistryError(f"{where} call_shapes names {cap!r}, which is not "
                                          f"one of its capabilities")
        if sig != KNOWN_CALL_SHAPES[cap]:
            raise CollectionRegistryError(f"{where} call shape {cap}={sig!r} is not the one "
                                          f"this build implements ({KNOWN_CALL_SHAPES[cap]})")
    runtime = raw["runtime_sha256"]
    if not isinstance(runtime, str) or not _SHA256.match(runtime):
        raise CollectionRegistryError(f"{where} runtime_sha256 must be 64 lowercase hex")
    accounts_raw = raw["accounts"]
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise CollectionRegistryError(f"{where} accounts must list at least one account version")
    accounts = []
    for j, acc in enumerate(accounts_raw):
        aw = f"{where}.accounts[{j}]"
        if not isinstance(acc, dict) or set(acc) != _ACCOUNT_KEYS:
            raise CollectionRegistryError(f"{aw} must have exactly {sorted(_ACCOUNT_KEYS)}")
        reg = _address(acc["registry"], f"{aw}.registry")
        impl = _address(acc["implementation"], f"{aw}.implementation")
        if reg != erc6551.REGISTRY.lower() or impl != erc6551.ACCOUNT_V3_IMPL.lower():
            raise CollectionRegistryError(
                f"{aw} is not the pinned ERC-6551 registry + AccountV3 — an account version "
                f"this build does not verify; refusing the registry")
        accounts.append(AccountVersion(reg, impl, _int(acc["salt"], f"{aw}.salt", minimum=0)))
    prefix = raw.get("journal_prefix")
    if prefix is not None and (not isinstance(prefix, str)
                               or not account_journal.JOURNAL_PREFIX_RE.fullmatch(prefix)):
        raise CollectionRegistryError(f"{where} journal_prefix {prefix!r} is not a valid prefix")
    return CollectionProfile(
        spec=spec, capabilities=tuple(caps),
        chain_id=_int(raw["chain_id"], f"{where}.chain_id", minimum=1),
        address=_address(raw["address"], f"{where}.address"),
        runtime_sha256=runtime,
        deploy_block=_int(raw["deploy_block"], f"{where}.deploy_block", minimum=0),
        max_supply=_int(raw["max_supply"], f"{where}.max_supply", minimum=1),
        accounts=tuple(accounts), journal_prefix=prefix)


def parse(data) -> Tuple[CollectionProfile, ...]:
    """Validate a registry document. Raises :class:`CollectionRegistryError` on anything off-schema."""
    if not isinstance(data, dict) or set(data) != {"profiles"} or not isinstance(data["profiles"], list):
        raise CollectionRegistryError('the registry must be exactly {"profiles": [...]}')
    out = tuple(_parse_profile(i, p) for i, p in enumerate(data["profiles"]))
    seen = set()
    for p in out:
        key = (p.chain_id, p.address)
        if key in seen:
            raise CollectionRegistryError(f"{p.address} on chain {p.chain_id} is pinned twice")
        seen.add(key)
    return out


def load(path: Optional[str] = None) -> Tuple[CollectionProfile, ...]:
    """The pinned profiles from *path* (default :data:`REGISTRY_FILE`). No file = ``()``."""
    path = path or REGISTRY_FILE
    if not os.path.exists(path):
        return ()
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() != 0 and os.access(path, os.W_OK):
        raise CollectionRegistryError(
            f"{path} is writable by this process — a pin the agent could write is no pin; "
            f"make it root-owned and read-only for the service identities")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CollectionRegistryError(f"{path} is unreadable: {exc}") from exc
    return parse(data)


def profiles() -> Tuple[CollectionProfile, ...]:
    """Every pinned profile (re-read per call). Raises when the file cannot be trusted."""
    return load()


def profiles_on(chain_id: int, capability: Optional[str] = None) -> Tuple[CollectionProfile, ...]:
    """The pinned profiles on *chain_id*, optionally only those declaring *capability*."""
    return tuple(p for p in profiles() if p.chain_id == int(chain_id)
                 and (capability is None or capability in p.capabilities))


def chain_id_of(chain: str) -> int:
    from core.wallet import chains
    row = chains.get(chain)
    return int(getattr(row, "chain_id", 0) or 0)


def profile_for(chain_id: int, address: str) -> Optional[CollectionProfile]:
    a = str(address or "").lower()
    return next((p for p in profiles_on(chain_id) if p.address == a), None)


def journal_prefixes() -> Tuple[str, ...]:
    """Every pinned profile's ``journal_prefix`` (raises when the file cannot be trusted)."""
    return tuple(p.journal_prefix for p in profiles() if p.journal_prefix)


def runtime_sha256_of(rpc, address: str) -> str:
    """sha256 (hex) of the runtime code at *address* (``eth_getCode`` at the head)."""
    import hashlib
    code = rpc("eth_getCode", [address, "latest"])
    if not isinstance(code, str) or not code.startswith("0x"):
        raise CollectionRegistryError(f"eth_getCode({address}) returned {code!r}")
    raw = bytes.fromhex(code[2:])
    if not raw:
        raise CollectionRegistryError(f"{address} has no code")
    return hashlib.sha256(raw).hexdigest()


def runtime_refusal(rpc, profile: CollectionProfile) -> Optional[str]:
    """Why the pinned collection's live code is NOT the pinned code, or None.

    Fail closed: an unreadable code read is a refusal, never a pass."""
    try:
        got = runtime_sha256_of(rpc, profile.address)
    except Exception as exc:  # noqa: BLE001
        return (f"the runtime code of the pinned collection {profile.address} could not be "
                f"read ({exc}) — its pin cannot be checked; failing closed")
    if got != profile.runtime_sha256:
        return (f"the runtime code of the pinned collection {profile.address} hashes to "
                f"{got[:16]}…, not the pinned {profile.runtime_sha256[:16]}… — the pin does not "
                f"describe this contract; refusing")
    return None
