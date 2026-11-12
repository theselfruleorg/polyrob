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
An unknown ``spec``, capability, account kind, standard or call shape refuses the whole file
(fail closed): an agent must never act on a profile it does not understand.

Schema ``agent-nft-profile/2`` (JSON)::

    {"profiles": [{
        "spec": "agent-nft-profile/2",
        "capabilities": ["mint", "reveal"],
        "chain_id": 4663,
        "address": "0x…",                       # the collection contract
        "runtime_sha256": "…64 hex…",           # its runtime code hash (recorded)
        "deploy_block": 123,
        "max_supply": 6551,                     # token ids are 1..max_supply
        "accounts": [                           # one entry per account kind the collection uses
            {"kind": "erc6551p", "registry": "0x0000…5758", "implementation": "0x…",
             "salt": 0, "runtime_sha256": "…64 hex…"},
            {"kind": "tokenbound_v3", "registry": "0x0000…5758",
             "implementation": "0x41C8…44eC", "salt": 0}],
        "standard": {"erc6551p": {              # optional: the collection's sale rails
            "market": "0x…", "market_runtime_sha256": "…",
            "zone": "0x…", "zone_runtime_sha256": "…"}},
        "journal_prefix": "…",                  # optional
        "journal_log": "0x…",                   # optional: the JournalLog contract (on-chain journal)
        "call_shapes": {"mint": "mint(address,uint256,uint256)",
                        "reveal": "reveal(uint256[])"},  # optional; must equal the known shapes
        "sale": {                               # REQUIRED with the `mint` capability, else absent/null
            "address": "0x…",                    # the collection's SALE() — the only public mint
            "runtime_sha256": "…64 hex…",        # its runtime code hash (its own pin)
            "max_price_wei": 99000000000000000}  # the most a public mint pays per token
    }]}

Account kinds (``core/wallet/account_kinds.py``). ``accounts[i].kind`` names a row of the kind
table; an unknown kind refuses the file. A kind with a built-in implementation
(``tokenbound_v3``) must name exactly it and carries no ``runtime_sha256`` (the built-in pin
wins); any other kind (``erc6551p``) REQUIRES its ``runtime_sha256``, re-checked against
``eth_getCode`` per call. ``standard.erc6551p`` (market and zone, each pinned by code hash) is
a COLLECTION fact and needs an ``erc6551p`` account; a 6551P collection with no market pins
without it.

Spec 1 (``agent-nft-profile/1``) is read too and mapped IN MEMORY to the same profile: its
accounts carry no ``kind``; without ``transfer_safe`` every account is ``tokenbound_v3``; with
it ``accounts[0]`` is ``erc6551p`` (``runtime_sha256`` = ``account_impl_runtime_sha256``) and
``market``/``zone`` become ``standard.erc6551p``. A spec-1 file and its spec-2 twin
(:func:`upgrade_document`) parse to EQUAL profiles. ``/nft trust`` writes spec 2.

The sale. The collection has no public mint: every mint goes through its ``SALE()`` (the
first deployment: polyrob-desk SPEC §6.7), so the ``mint`` capability REQUIRES the ``sale`` key
and ``sale`` without ``mint`` refuses. Every key of ``sale`` is required; an unknown one
refuses. A sale address equal to a collection address, or to another profile's sale, on the
same chain refuses the file. The sale's code has its own pin (:func:`sale_runtime_refusal`),
checked beside the collection's before a mint.

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
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from core.wallet.account_kinds import ERC6551P, KINDS, TOKENBOUND_V3, AccountKind, kind_named

logger = logging.getLogger(__name__)

#: The owner-written registry file (root-owned; the agent cannot write it).
REGISTRY_FILE = "/etc/polyrob/agent_nft_collections.json"

SPEC_V1 = "agent-nft-profile/1"
SPEC_V2 = "agent-nft-profile/2"
#: The profile specs this code understands. Anything else refuses the whole file. Spec 1 is
#: mapped to spec 2 in memory (one release cycle; ``polyrob nft profile upgrade``).
KNOWN_SPECS = frozenset({SPEC_V1, SPEC_V2})
CAP_MINT = "mint"
CAP_REVEAL = "reveal"
#: The capabilities this code understands.
KNOWN_CAPABILITIES = frozenset({CAP_MINT, CAP_REVEAL})
#: Each call shape core implements (a ``call_shapes`` key), and the capability it belongs to:
#: the ``mint`` capability is the sale's public ``mint`` AND its whitelist ``mintWL``.
SHAPE_MINT_WL = "mint_wl"
KNOWN_CALL_SHAPES: Dict[str, str] = {
    CAP_MINT: "mint(address,uint256,uint256)",
    SHAPE_MINT_WL: "mintWL(address,uint256,uint256,uint256,bytes32[])",
    CAP_REVEAL: "reveal(uint256[])",
}
_SHAPE_CAPABILITY: Dict[str, str] = {CAP_MINT: CAP_MINT, SHAPE_MINT_WL: CAP_MINT,
                                     CAP_REVEAL: CAP_REVEAL}

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMON_KEYS = frozenset({"spec", "capabilities", "chain_id", "address", "runtime_sha256",
                          "deploy_block", "max_supply", "accounts", "journal_prefix",
                          "call_shapes", "journal_log", "sale"})
_REQUIRED_KEYS = _COMMON_KEYS - {"journal_prefix", "call_shapes", "journal_log", "sale"}
#: The keys of each spec (an unknown key refuses the file).
_SPEC_KEYS = {SPEC_V1: _COMMON_KEYS | {"transfer_safe"}, SPEC_V2: _COMMON_KEYS | {"standard"}}
#: Every key of ``sale`` is required.
_SALE_KEYS = frozenset({"address", "runtime_sha256", "max_price_wei"})
_ACCOUNT_KEYS_V1 = frozenset({"registry", "implementation", "salt"})
_ACCOUNT_KEYS_V2 = _ACCOUNT_KEYS_V1 | {"kind"}
#: Spec 1 only: every key of ``transfer_safe`` is required.
_TRANSFER_SAFE_KEYS = frozenset({"account_impl_runtime_sha256", "market", "market_runtime_sha256",
                                 "zone", "zone_runtime_sha256"})
#: Spec 2: the collection standards a profile may pin, and the keys of each (all required).
_ERC6551P_STANDARD_KEYS = frozenset({"market", "market_runtime_sha256", "zone",
                                     "zone_runtime_sha256"})
_KNOWN_STANDARDS = frozenset({"erc6551p"})


class CollectionRegistryError(RuntimeError):
    """The registry file exists but cannot be trusted (unreadable, writable, or off-schema)."""


@dataclass(frozen=True)
class AccountVersion:
    registry: str        # lowercase
    implementation: str  # lowercase
    salt: int
    #: The row of ``core.wallet.account_kinds`` (``accounts[i].kind``).
    kind: AccountKind = TOKENBOUND_V3
    #: The pinned runtime code hash of the implementation. None = a kind with a built-in pin
    #: (``kind.builtin_code_sha256``).
    runtime_sha256: Optional[str] = None


@dataclass(frozen=True)
class Erc6551pStandard:
    """``standard.erc6551p``: the sale rails of an ERC-6551P collection (lowercase / 64 hex)."""
    market: str
    market_runtime_sha256: str
    zone: str
    zone_runtime_sha256: str


@dataclass(frozen=True)
class Sale:
    """The pinned sale of one collection (the first deployment: polyrob-desk SPEC §6.7)."""
    address: str          # lowercase
    runtime_sha256: str   # 64 hex
    max_price_wei: int    # > 0: the most a public mint pays per token


@dataclass(frozen=True)
class CollectionProfile:
    #: The spec the profile is held in, in memory: always :data:`SPEC_V2` (spec 1 is mapped).
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
    #: ``standard.erc6551p``: the collection's market and zone; None = none pinned.
    erc6551p: Optional[Erc6551pStandard] = None
    #: The collection's sale, the only public mint (required with the ``mint`` capability).
    sale: Optional[Sale] = None
    #: The spec the file was WRITTEN in (``spec`` is the in-memory form). Not compared.
    source_spec: str = field(default=SPEC_V2, compare=False)

    def account_version(self, implementation: Optional[str]) -> Optional[AccountVersion]:
        """The pinned account version whose implementation is *implementation*, or None."""
        impl = str(implementation or "").lower()
        return next((v for v in self.accounts if v.implementation == impl), None)

    def version_for_account(self, token_id: int, account: str) -> Optional[AccountVersion]:
        """THE rule from an account address to its kind: the pinned account version whose
        CREATE2 account of *token_id* is *account*, or None."""
        from core.wallet import erc6551
        a = str(account or "").lower()
        return next((v for v in self.accounts if erc6551.account_address(
            self.chain_id, self.address, int(token_id), salt=v.salt,
            implementation=v.implementation).lower() == a), None)

    def account_of(self, token_id: int) -> Tuple[str, AccountVersion]:
        """``(account, version)`` of *token_id* when no account is named yet: the FIRST pinned
        version's (``accounts[0]``, the collection's own). Every later step names the account and
        goes through :meth:`version_for_account`."""
        from core.wallet import erc6551
        v = self.accounts[0]
        return erc6551.account_address(self.chain_id, self.address, int(token_id), salt=v.salt,
                                       implementation=v.implementation), v


def _int(value, what: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CollectionRegistryError(f"{what} must be an integer >= {minimum}, got {value!r}")
    return value


def _address(value, what: str) -> str:
    if not isinstance(value, str) or not _ADDRESS.match(value):
        raise CollectionRegistryError(f"{what} must be a 0x address, got {value!r}")
    return value.lower()


def _spec1_as_spec2(raw: dict) -> dict:
    """A spec-1 profile, as the spec-2 profile that means the same (pure; never raises on a
    well-formed spec-1 row — the spec-2 parser then judges the result)."""
    out = {k: v for k, v in raw.items() if k != "transfer_safe"}
    out["spec"] = SPEC_V2
    ts = raw.get("transfer_safe")
    accounts = []
    for j, acc in enumerate(raw.get("accounts") or []):
        if not isinstance(acc, dict):
            accounts.append(acc)
            continue
        row = {"kind": TOKENBOUND_V3.name, **acc}
        if j == 0 and isinstance(ts, dict):
            row = {"kind": ERC6551P.name, **acc,
                   "runtime_sha256": ts.get("account_impl_runtime_sha256")}
        accounts.append(row)
    out["accounts"] = accounts
    if isinstance(ts, dict):
        out["standard"] = {ERC6551P.name: {k: ts.get(k) for k in sorted(_ERC6551P_STANDARD_KEYS)}}
    return out


def upgrade_profile(raw: dict) -> dict:
    """The spec-2 form of one raw profile (a spec-2 profile is returned unchanged). Raises
    :class:`CollectionRegistryError` when *raw* is not a valid profile of a known spec."""
    if not isinstance(raw, dict) or raw.get("spec") not in KNOWN_SPECS:
        raise CollectionRegistryError(f"not a profile of a known spec: {raw!r:.80}")
    if raw["spec"] == SPEC_V2:
        return dict(raw)
    if "transfer_safe" in raw and (not isinstance(raw["transfer_safe"], dict)
                                   or set(raw["transfer_safe"]) != _TRANSFER_SAFE_KEYS):
        raise CollectionRegistryError(
            f"transfer_safe must have exactly {sorted(_TRANSFER_SAFE_KEYS)}")
    return _spec1_as_spec2(raw)


def upgrade_document(data) -> dict:
    """The spec-2 form of a whole registry document; checked: both parse to EQUAL profiles."""
    if not isinstance(data, dict) or set(data) != {"profiles"} or not isinstance(data["profiles"], list):
        raise CollectionRegistryError('the registry must be exactly {"profiles": [...]}')
    out = {"profiles": [upgrade_profile(p) for p in data["profiles"]]}
    if parse(out) != parse(data):
        raise CollectionRegistryError("the spec-2 form does not parse to the same profiles")
    return out


def _parse_profile(i: int, raw) -> CollectionProfile:
    from core.wallet import account_journal
    where = f"profiles[{i}]"
    if not isinstance(raw, dict):
        raise CollectionRegistryError(f"{where} is not an object")
    spec = raw.get("spec")
    if spec not in KNOWN_SPECS:
        raise CollectionRegistryError(f"{where} spec {spec!r} is unknown (this build knows "
                                      f"{sorted(KNOWN_SPECS)}) — refusing the registry")
    unknown = set(raw) - _SPEC_KEYS[spec]
    if unknown:
        raise CollectionRegistryError(f"{where} has unknown field(s) {sorted(unknown)}")
    missing = _REQUIRED_KEYS - set(raw)
    if missing:
        raise CollectionRegistryError(f"{where} is missing {sorted(missing)}")
    if spec == SPEC_V1:
        for j, acc in enumerate(raw["accounts"] if isinstance(raw["accounts"], list) else []):
            if not isinstance(acc, dict) or set(acc) != _ACCOUNT_KEYS_V1:
                raise CollectionRegistryError(
                    f"{where}.accounts[{j}] must have exactly {sorted(_ACCOUNT_KEYS_V1)}")
        if "transfer_safe" in raw:
            _transfer_safe_v1(raw["transfer_safe"], f"{where}.transfer_safe")
        raw = _spec1_as_spec2(raw)
    caps = raw["capabilities"]
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        raise CollectionRegistryError(f"{where} capabilities must be a list of names")
    bad = [c for c in caps if c not in KNOWN_CAPABILITIES]
    if bad:
        raise CollectionRegistryError(f"{where} capability {bad} is unknown (this build knows "
                                      f"{sorted(KNOWN_CAPABILITIES)}) — refusing the registry")
    shapes = raw.get("call_shapes", {})
    if not isinstance(shapes, dict):
        raise CollectionRegistryError(f"{where} call_shapes must be an object")
    for cap, sig in shapes.items():
        if cap not in KNOWN_CALL_SHAPES:
            raise CollectionRegistryError(f"{where} call shape {cap!r} is unknown (this build "
                                          f"knows {sorted(KNOWN_CALL_SHAPES)}) — refusing the "
                                          f"registry")
        if _SHAPE_CAPABILITY[cap] not in caps:
            raise CollectionRegistryError(f"{where} call_shapes names {cap!r}, which is not "
                                          f"one of its capabilities")
        if sig != KNOWN_CALL_SHAPES[cap]:
            raise CollectionRegistryError(f"{where} call shape {cap}={sig!r} is not the one "
                                          f"this build implements ({KNOWN_CALL_SHAPES[cap]})")
    runtime = _sha(raw["runtime_sha256"], f"{where} runtime_sha256")
    sale = None
    if raw.get("sale") is not None or (spec == SPEC_V1 and "sale" in raw):
        sale = _sale(raw["sale"], f"{where}.sale")
    if CAP_MINT in caps and sale is None:
        raise CollectionRegistryError(
            f"{where} declares the mint capability without a sale — the collection has no public "
            f"mint, every mint goes through its pinned SALE(); refusing the registry")
    if sale is not None and CAP_MINT not in caps:
        raise CollectionRegistryError(f"{where} pins a sale without the mint capability")
    accounts = _accounts(raw["accounts"], where)
    erc6551p = _standard(raw["standard"], f"{where}.standard") if "standard" in raw else None
    if erc6551p is not None and not any(v.kind is ERC6551P for v in accounts):
        raise CollectionRegistryError(f"{where}.standard.erc6551p pins sale rails, but no account "
                                      f"version is an {ERC6551P.label} — refusing the registry")
    prefix = raw.get("journal_prefix")
    if prefix is not None and (not isinstance(prefix, str)
                               or not account_journal.JOURNAL_PREFIX_RE.fullmatch(prefix)):
        raise CollectionRegistryError(f"{where} journal_prefix {prefix!r} is not a valid prefix")
    journal_log = (_address(raw["journal_log"], f"{where}.journal_log")
                   if "journal_log" in raw else None)
    return CollectionProfile(
        spec=SPEC_V2, capabilities=tuple(caps),
        chain_id=_int(raw["chain_id"], f"{where}.chain_id", minimum=1),
        address=_address(raw["address"], f"{where}.address"),
        runtime_sha256=runtime,
        deploy_block=_int(raw["deploy_block"], f"{where}.deploy_block", minimum=0),
        max_supply=_int(raw["max_supply"], f"{where}.max_supply", minimum=1),
        accounts=accounts, journal_prefix=prefix, journal_log=journal_log,
        erc6551p=erc6551p, sale=sale, source_spec=spec)


def _accounts(accounts_raw, where: str) -> Tuple[AccountVersion, ...]:
    from core.wallet import erc6551
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise CollectionRegistryError(f"{where} accounts must list at least one account version")
    out = []
    for j, acc in enumerate(accounts_raw):
        aw = f"{where}.accounts[{j}]"
        if not isinstance(acc, dict) or not _ACCOUNT_KEYS_V2 <= set(acc) \
                or set(acc) - _ACCOUNT_KEYS_V2 - {"runtime_sha256"}:
            raise CollectionRegistryError(f"{aw} must have exactly {sorted(_ACCOUNT_KEYS_V2)} "
                                          f"(and runtime_sha256 for a kind without a built-in pin)")
        kind = kind_named(acc["kind"]) if isinstance(acc["kind"], str) else None
        if kind is None:
            raise CollectionRegistryError(
                f"{aw}.kind {acc['kind']!r} is not an account kind this build verifies "
                f"({sorted(KINDS)}) — refusing the registry")
        reg = _address(acc["registry"], f"{aw}.registry")
        impl = _address(acc["implementation"], f"{aw}.implementation")
        salt = _int(acc["salt"], f"{aw}.salt", minimum=0)
        if reg != erc6551.REGISTRY.lower():
            raise CollectionRegistryError(
                f"{aw} is not the pinned ERC-6551 registry — an account version this build does "
                f"not verify; refusing the registry")
        if kind.builtin_implementation:
            if impl != kind.builtin_implementation.lower():
                raise CollectionRegistryError(
                    f"{aw} names kind {kind.name}, whose one implementation is "
                    f"{kind.builtin_implementation} ({kind.label}), not {impl} — an account "
                    f"version this build does not verify; refusing the registry")
            if "runtime_sha256" in acc:
                raise CollectionRegistryError(
                    f"{aw} carries a runtime_sha256, but the {kind.label} code is pinned by "
                    f"this build — refusing the registry")
            out.append(AccountVersion(reg, impl, salt, kind))
            continue
        if impl == TOKENBOUND_V3.builtin_implementation.lower():
            raise CollectionRegistryError(
                f"{aw} names kind {kind.name}, but its implementation is AccountV3 "
                f"({TOKENBOUND_V3.label}) — refusing the registry")
        if "runtime_sha256" not in acc:
            raise CollectionRegistryError(f"{aw} ({kind.label}) needs its runtime_sha256")
        out.append(AccountVersion(reg, impl, salt, kind,
                                  _sha(acc["runtime_sha256"], f"{aw}.runtime_sha256")))
    return tuple(out)


def _sha(value, what: str) -> str:
    if not isinstance(value, str) or not _SHA256.match(value):
        raise CollectionRegistryError(f"{what} must be 64 lowercase hex")
    return value


def _transfer_safe_v1(raw, where: str) -> None:
    if not isinstance(raw, dict) or set(raw) != _TRANSFER_SAFE_KEYS:
        raise CollectionRegistryError(f"{where} must have exactly {sorted(_TRANSFER_SAFE_KEYS)}")
    _sha(raw["account_impl_runtime_sha256"], f"{where}.account_impl_runtime_sha256")


def _standard(raw, where: str) -> Optional[Erc6551pStandard]:
    if not isinstance(raw, dict) or not raw:
        raise CollectionRegistryError(f"{where} must be an object naming a collection standard")
    unknown = set(raw) - _KNOWN_STANDARDS
    if unknown:
        raise CollectionRegistryError(f"{where} names unknown standard(s) {sorted(unknown)} "
                                      f"(this build knows {sorted(_KNOWN_STANDARDS)})")
    std = raw["erc6551p"]
    w = f"{where}.erc6551p"
    if not isinstance(std, dict) or set(std) != _ERC6551P_STANDARD_KEYS:
        raise CollectionRegistryError(f"{w} must have exactly {sorted(_ERC6551P_STANDARD_KEYS)}")
    return Erc6551pStandard(
        market=_address(std["market"], f"{w}.market"),
        market_runtime_sha256=_sha(std["market_runtime_sha256"], f"{w}.market_runtime_sha256"),
        zone=_address(std["zone"], f"{w}.zone"),
        zone_runtime_sha256=_sha(std["zone_runtime_sha256"], f"{w}.zone_runtime_sha256"))


def _sale(raw, where: str) -> Sale:
    if not isinstance(raw, dict) or set(raw) != _SALE_KEYS:
        raise CollectionRegistryError(f"{where} must have exactly {sorted(_SALE_KEYS)}")
    return Sale(address=_address(raw["address"], f"{where}.address"),
                runtime_sha256=_sha(raw["runtime_sha256"], f"{where}.runtime_sha256"),
                max_price_wei=_int(raw["max_price_wei"], f"{where}.max_price_wei", minimum=1))


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
    sales = set()
    for p in out:
        if p.sale is None:
            continue
        key = (p.chain_id, p.sale.address)
        if key in seen:
            raise CollectionRegistryError(f"the sale {p.sale.address} of {p.address} on chain "
                                          f"{p.chain_id} is also a pinned collection")
        if key in sales:
            raise CollectionRegistryError(f"the sale {p.sale.address} on chain {p.chain_id} is "
                                          f"pinned by two profiles")
        sales.add(key)
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
    if p.sale is not None:
        return f"{p.address} carries a sale (only the admin file pins one)"
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


def profile_for_sale(chain_id: int, address: str) -> Optional[CollectionProfile]:
    """The pinned profile whose ``sale`` is *address* on *chain_id*, or None."""
    a = str(address or "").lower()
    return next((p for p in profiles_on(chain_id)
                 if p.sale is not None and p.sale.address == a), None)


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


def code_pin_refusal(rpc, address: str, expected_sha256: str,
                     what: str = "pinned collection") -> Optional[str]:
    """Why the live code at *address* does NOT hash to *expected_sha256*, or None.

    Fail closed: an unreadable code read is a refusal, never a pass."""
    try:
        got = runtime_sha256_of(rpc, address)
    except Exception as exc:  # noqa: BLE001
        return (f"the runtime code of the {what} {address} could not be "
                f"read ({exc}) — its pin cannot be checked; failing closed")
    if got != expected_sha256:
        return (f"the runtime code of the {what} {address} hashes to "
                f"{got[:16]}…, not the pinned {expected_sha256[:16]}… — the pin does not "
                f"describe this contract; refusing")
    return None


def runtime_refusal(rpc, profile: CollectionProfile) -> Optional[str]:
    """Why the pinned collection's live code is NOT the pinned code, or None."""
    return code_pin_refusal(rpc, profile.address, profile.runtime_sha256)


def sale_runtime_refusal(rpc, profile: CollectionProfile) -> Optional[str]:
    """Why the pinned SALE's live code is NOT its pinned code, or None (a profile without a
    sale refuses: nothing pins the contract a mint would pay)."""
    if profile.sale is None:
        return f"the pinned collection {profile.address} pins no sale"
    return code_pin_refusal(rpc, profile.sale.address, profile.sale.runtime_sha256,
                            "pinned sale")
