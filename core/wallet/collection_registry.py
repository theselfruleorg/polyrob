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
        "journal_log": "0x…",                   # optional: the JournalLog contract (on-chain journal)
        "call_shapes": {"mint": "mint(address,uint256,uint256)",
                        "reveal": "reveal(uint256[])"},  # optional; must equal the known shapes
        "transfer_safe": {                      # optional: the collection's own PolyrobAccount
            "account_impl_runtime_sha256": "…",  # accounts[0].implementation's runtime code hash
            "market": "0x…", "market_runtime_sha256": "…",
            "zone": "0x…", "zone_runtime_sha256": "…"}
    }]}

Account versions. Without ``transfer_safe`` every ``accounts[]`` entry is the canonical registry
+ Tokenbound AccountV3 (pinned by :data:`core.wallet.erc6551.CODE_SHA256`). With it,
``accounts[0]`` is the collection's PolyrobAccount implementation (any address, pinned by
``transfer_safe.account_impl_runtime_sha256``, re-checked against ``eth_getCode`` per call); any
further entry is still AccountV3. A PolyrobAccount has no lock, no ERC-2771 forwarder and no
ERC-1271 signer, and at rest it holds no modeled grant (polyrob-desk SPEC §7.1): the guard and
the views branch on :attr:`AccountVersion.kind`.

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
                           "call_shapes", "journal_log", "transfer_safe"})
_REQUIRED_KEYS = _PROFILE_KEYS - {"journal_prefix", "call_shapes", "journal_log", "transfer_safe"}
_ACCOUNT_KEYS = frozenset({"registry", "implementation", "salt"})
#: Every key of ``transfer_safe`` is required (``polyrob-desk/contracts/script/pin.py`` writes it).
_TRANSFER_SAFE_KEYS = frozenset({"account_impl_runtime_sha256", "market", "market_runtime_sha256",
                                 "zone", "zone_runtime_sha256"})

#: The account kinds this build verifies (``AccountVersion.kind``).
KIND_ACCOUNT_V3 = "account_v3"
KIND_POLYROB = "polyrob_account"


class CollectionRegistryError(RuntimeError):
    """The registry file exists but cannot be trusted (unreadable, writable, or off-schema)."""


@dataclass(frozen=True)
class AccountVersion:
    registry: str        # lowercase
    implementation: str  # lowercase
    salt: int
    #: :data:`KIND_ACCOUNT_V3` (Tokenbound AccountV3) or :data:`KIND_POLYROB` (PolyrobAccount).
    kind: str = KIND_ACCOUNT_V3
    #: The pinned runtime code hash of the implementation. None = AccountV3, pinned by
    #: ``erc6551.CODE_SHA256``.
    runtime_sha256: Optional[str] = None

    @property
    def is_polyrob(self) -> bool:
        return self.kind == KIND_POLYROB


@dataclass(frozen=True)
class TransferSafe:
    """The PolyrobAccount pins of one collection (all lowercase / 64 hex)."""
    account_impl_runtime_sha256: str
    market: str
    market_runtime_sha256: str
    zone: str
    zone_runtime_sha256: str


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
    #: J2: the pinned ``JournalLog`` (``log(bytes)`` → ``Entry(address indexed account, bytes)``)
    #: this collection's accounts write their journal to, in the same ``executeBatch`` as the
    #: action. None = no on-chain journal (the local file only).
    journal_log: Optional[str] = None  # lowercase
    #: The collection's own transfer-safe account (PolyrobAccount), its market and zone; None
    #: = the collection's accounts are Tokenbound AccountV3.
    transfer_safe: Optional[TransferSafe] = None

    def account_version(self, implementation: Optional[str]) -> Optional[AccountVersion]:
        """The pinned account version whose implementation is *implementation*, or None."""
        impl = str(implementation or "").lower()
        return next((v for v in self.accounts if v.implementation == impl), None)

    def version_for_account(self, token_id: int, account: str) -> Optional[AccountVersion]:
        """The pinned account version whose CREATE2 account of *token_id* is *account*, or None."""
        from core.wallet import erc6551
        a = str(account or "").lower()
        return next((v for v in self.accounts if erc6551.account_address(
            self.chain_id, self.address, int(token_id), salt=v.salt,
            implementation=v.implementation).lower() == a), None)


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
    runtime = _sha(raw["runtime_sha256"], f"{where} runtime_sha256")
    transfer_safe = _transfer_safe(raw["transfer_safe"], f"{where}.transfer_safe") \
        if "transfer_safe" in raw else None
    accounts_raw = raw["accounts"]
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise CollectionRegistryError(f"{where} accounts must list at least one account version")
    accounts = []
    v3 = erc6551.ACCOUNT_V3_IMPL.lower()
    for j, acc in enumerate(accounts_raw):
        aw = f"{where}.accounts[{j}]"
        if not isinstance(acc, dict) or set(acc) != _ACCOUNT_KEYS:
            raise CollectionRegistryError(f"{aw} must have exactly {sorted(_ACCOUNT_KEYS)}")
        reg = _address(acc["registry"], f"{aw}.registry")
        impl = _address(acc["implementation"], f"{aw}.implementation")
        salt = _int(acc["salt"], f"{aw}.salt", minimum=0)
        if reg != erc6551.REGISTRY.lower():
            raise CollectionRegistryError(
                f"{aw} is not the pinned ERC-6551 registry — an account version this build does "
                f"not verify; refusing the registry")
        if j == 0 and transfer_safe is not None:
            if impl == v3:
                raise CollectionRegistryError(
                    f"{aw} is AccountV3, but transfer_safe pins a PolyrobAccount implementation "
                    f"as accounts[0] — refusing the registry")
            accounts.append(AccountVersion(reg, impl, salt, KIND_POLYROB,
                                           transfer_safe.account_impl_runtime_sha256))
            continue
        if impl != v3:
            raise CollectionRegistryError(
                f"{aw} is not the pinned ERC-6551 registry + AccountV3 (and not accounts[0] of a "
                f"profile with transfer_safe) — an account version this build does not verify; "
                f"refusing the registry")
        accounts.append(AccountVersion(reg, impl, salt))
    prefix = raw.get("journal_prefix")
    if prefix is not None and (not isinstance(prefix, str)
                               or not account_journal.JOURNAL_PREFIX_RE.fullmatch(prefix)):
        raise CollectionRegistryError(f"{where} journal_prefix {prefix!r} is not a valid prefix")
    journal_log = (_address(raw["journal_log"], f"{where}.journal_log")
                   if "journal_log" in raw else None)
    return CollectionProfile(
        spec=spec, capabilities=tuple(caps),
        chain_id=_int(raw["chain_id"], f"{where}.chain_id", minimum=1),
        address=_address(raw["address"], f"{where}.address"),
        runtime_sha256=runtime,
        deploy_block=_int(raw["deploy_block"], f"{where}.deploy_block", minimum=0),
        max_supply=_int(raw["max_supply"], f"{where}.max_supply", minimum=1),
        accounts=tuple(accounts), journal_prefix=prefix, journal_log=journal_log,
        transfer_safe=transfer_safe)


def _sha(value, what: str) -> str:
    if not isinstance(value, str) or not _SHA256.match(value):
        raise CollectionRegistryError(f"{what} must be 64 lowercase hex")
    return value


def _transfer_safe(raw, where: str) -> TransferSafe:
    if not isinstance(raw, dict) or set(raw) != _TRANSFER_SAFE_KEYS:
        raise CollectionRegistryError(f"{where} must have exactly {sorted(_TRANSFER_SAFE_KEYS)}")
    return TransferSafe(
        account_impl_runtime_sha256=_sha(raw["account_impl_runtime_sha256"],
                                         f"{where}.account_impl_runtime_sha256"),
        market=_address(raw["market"], f"{where}.market"),
        market_runtime_sha256=_sha(raw["market_runtime_sha256"], f"{where}.market_runtime_sha256"),
        zone=_address(raw["zone"], f"{where}.zone"),
        zone_runtime_sha256=_sha(raw["zone_runtime_sha256"], f"{where}.zone_runtime_sha256"))


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


#: The owner's chat pins (``/nft trust``, ``core/wallet/collection_trust.py``), beside the
#: token pins in the wallet data home. The agent's file tools are denied it
#: (``core/security/secret_guard.py``); only an owner seat writes it.
OWNER_PINS_NAME = "collection_pins.json"


def owner_pins_path(data_home: Optional[str] = None) -> str:
    """``<data_home>/wallet/collection_pins.json`` — the token pins' rule."""
    if data_home:
        return os.path.join(str(data_home), "wallet", OWNER_PINS_NAME)
    from core.wallet.audit_sink import _wallet_data_dir
    return os.path.join(_wallet_data_dir(), OWNER_PINS_NAME)


#: The largest supply an owner pin may carry. The guard's nesting rule derives the account
#: address of EVERY id ``0..max_supply`` (``erc6551.collection_account_addresses``); an
#: uncapped collection would hang every NFT move. A larger collection is the admin's to pin.
MAX_OWNER_SUPPLY = 20_000


def _owner_shape_refusal(p: CollectionProfile) -> Optional[str]:
    """Why an owner-store profile is not the shape ``/nft trust`` writes, or None. Checked on
    READ, not only by the writer: a store the agent forged through a shell must not arm a mint,
    a reveal or an on-chain journal target, or carry a supply that hangs the guard."""
    if p.capabilities:
        return f"{p.address} carries capabilities {list(p.capabilities)} (only the admin file grants them)"
    if p.journal_log or p.journal_prefix:
        return f"{p.address} carries a journal setting (only the admin file sets one)"
    if p.max_supply > MAX_OWNER_SUPPLY:
        return f"{p.address} max_supply {p.max_supply} is above {MAX_OWNER_SUPPLY}"
    return None


def load_owner_pins(path: Optional[str] = None) -> Tuple[CollectionProfile, ...]:
    """The owner's chat pins. No file = ``()``; an unreadable, off-schema or off-shape file
    RAISES (the whole store: one forged row poisons it all)."""
    path = path or owner_pins_path()
    if not os.path.exists(path):
        return ()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CollectionRegistryError(f"{path} is unreadable: {exc}") from exc
    out = parse(data)
    for p in out:
        why = _owner_shape_refusal(p)
        if why:
            raise CollectionRegistryError(f"{path}: {why} — refusing the owner pins")
    return out


def pinned_with_source(*, strict: bool = True) -> Tuple[Tuple[CollectionProfile, str], ...]:
    """Every pin with who made it: ``"admin"`` (the root file) or ``"owner"`` (``/nft trust``).
    A collection in both is the admin's. The admin file always raises when it cannot be
    trusted. A bad OWNER store raises when *strict* (the views: unreadable is not empty);
    otherwise its pins are left out with a warning, so a broken chat store never takes the
    admin's pins down with it — leaving an owner pin out only refuses acts through it."""
    admin = load()
    seen = {(p.chain_id, p.address) for p in admin}
    try:
        owner_all = load_owner_pins()
    except CollectionRegistryError:
        if strict:
            raise
        logger.warning("owner collection pins unreadable; acting on the admin pins only",
                       exc_info=True)
        owner_all = ()
    owner = tuple(p for p in owner_all if (p.chain_id, p.address) not in seen)
    return tuple((p, "admin") for p in admin) + tuple((p, "owner") for p in owner)


def profiles() -> Tuple[CollectionProfile, ...]:
    """Every pinned profile (re-read per call): the root file's, then the owner's chat pins.
    Raises when the admin file cannot be trusted; a bad owner store drops only its own pins."""
    return tuple(p for p, _src in pinned_with_source(strict=False))


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
