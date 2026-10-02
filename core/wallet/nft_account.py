"""Work from an NFT's account as its owner (069 v4 A3): resolve the target, then journal the act.

069 v4 §2 — the simple model: an NFT's ERC-6551 account is controlled by whoever owns the NFT,
and this instance acts through an account ONLY when its treasury owns the NFT. A money verb
given ``account=<token-bound account>`` or ``nft=<collection>#<id>`` resolves it HERE, then
builds a ``TxIntent.via_account`` for ``tx_guard`` (the ONE authorizer, which re-reads the owner,
the lock, the code pins and ``state()`` in its own pre-flight), books the result against the
account (the ``account`` column) and appends a signed entry to the account's journal.

:func:`resolve` refuses (``NftAccountError``) unless — read now, fail closed:

* the collection is PINNED in the owner's collection registry on that chain (an untrusted
  registry pins nothing), and its live code hashes to the pinned ``runtime_sha256``;
* the token id is within the profile's ``max_supply``, and the account is the CREATE2 address
  of one of the profile's pinned account versions (an ``account=`` is read back with
  ``token()`` and must name the same token);
* the account is deployed;
* ``ownerOf(id)`` IS this treasury.

The journal (§5 rule 6): one JSONL file per account in the instance's self tier,
``agent_nft/journal/<chain_id>-<account>.jsonl``. An entry carries ``seq, ts, account,
chain_id, kind, text, refs, positions_after, owner, prev`` (``prev`` = sha256 of the previous
entry's canonical bytes, ``"genesis"`` first) and ``sig`` = EIP-191 by the owner key over
:func:`core.wallet.account_journal.journal_payload`, whose ``text_sha256`` slot carries sha256 of
the canonical entry WITHOUT ``sig`` — so the signature covers every field. The same format the
agent-NFT package verifies. Prose is data: a reader wraps it as untrusted.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Rpc = Callable[[str, list], Any]

GENESIS = "genesis"
JOURNAL_DIR = ("agent_nft", "journal")

_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_NFT_RE = re.compile(r"^\s*(?:(0x[0-9a-fA-F]{40})\s*[#/:]\s*|#\s*)?(\d{1,12})\s*$")


class NftAccountError(RuntimeError):
    """The account/NFT cannot be acted through by this instance (named reason)."""


@dataclass(frozen=True)
class HeldNft:
    """An NFT of a pinned collection that THIS treasury owns, and its account."""
    chain: str
    chain_id: int
    collection: str        # lowercase
    token_id: int
    account: str           # checksummed
    journal_prefix: Optional[str] = None

    @property
    def label(self) -> str:
        name = self.journal_prefix or f"{self.collection[:10]}…"
        return f"{name} #{self.token_id}"

    @property
    def key(self) -> str:
        return f"{self.chain_id}:{self.collection}:{self.token_id}"


def chain_name_for_id(chain_id: int) -> Optional[str]:
    """The EVM chain row name for *chain_id* (``robinhood`` for 4663), or None."""
    from core.wallet import chains
    for row in chains.evm_rows():
        if int(getattr(row, "chain_id", 0) or 0) == int(chain_id):
            return row.name
    return None


def parse_nft(text: str) -> Tuple[Optional[str], int]:
    """``"<0xcollection>#<id>"`` / ``"<0xcollection>/<id>"`` / ``"#<id>"`` / ``"<id>"`` ->
    ``(collection or None, id)``. Raises :class:`NftAccountError` on anything else."""
    m = _NFT_RE.match(str(text or ""))
    if not m:
        raise NftAccountError(f"{text!r} is not an NFT reference — use <collection>#<id> or <id>")
    return (m.group(1).lower() if m.group(1) else None), int(m.group(2))


def read_owner_of(rpc: Rpc, collection: str, token_id: int) -> str:
    from core.wallet import abi
    raw = rpc("eth_call", [{"to": collection, "data": abi.encode_call(
        "ownerOf", [{"type": "uint256"}], [int(token_id)])}, "latest"])
    return str(abi.decode([{"type": "address"}], raw)[0])


def _profiles(chain_id: int):
    from core.wallet import collection_registry
    try:
        return collection_registry.profiles_on(chain_id)
    except Exception as exc:  # noqa: BLE001 — an untrusted registry pins nothing
        raise NftAccountError(f"the owner's collection registry cannot be trusted ({exc}) — "
                              f"no account can be acted through until the owner fixes it") from exc


def _account_of(chain_id: int, profile, token_id: int) -> List[str]:
    from core.wallet import erc6551
    return [erc6551.account_address(chain_id, profile.address, int(token_id), salt=v.salt,
                                    implementation=v.implementation) for v in profile.accounts]


def resolve(chain: str, *, rpc: Rpc, treasury: str, account: Optional[str] = None,
            nft: Optional[str] = None, check_owner: bool = True) -> HeldNft:
    """The pinned NFT named by ``account=`` and/or ``nft=``, owned by *treasury*. Raises
    :class:`NftAccountError` with the reason otherwise (see the module doc)."""
    from core.wallet import collection_registry, erc6551
    if not account and not nft:
        raise NftAccountError("name the NFT: account=<token-bound account> or nft=<collection>#<id>")
    chain_id = collection_registry.chain_id_of(chain)
    if not chain_id:
        raise NftAccountError(f"{chain!r} is not an EVM chain this build knows")
    profiles = _profiles(chain_id)
    if not profiles:
        raise NftAccountError(f"no collection is pinned on {chain} (the owner's collection registry "
                              f"is empty by default) — an agent acts only through accounts of a "
                              f"pinned collection")
    by_addr = {p.address: p for p in profiles}
    profile = None
    token_id = None
    if nft:
        collection, token_id = parse_nft(nft)
        if collection is None:
            if len(profiles) != 1:
                raise NftAccountError(
                    f"{len(profiles)} collections are pinned on {chain}: name one as "
                    f"<collection>#{token_id} ({', '.join(sorted(by_addr))})")
            profile = profiles[0]
        else:
            profile = by_addr.get(collection)
            if profile is None:
                raise NftAccountError(f"{collection} is not a pinned collection on {chain}")
    if account:
        if not _ADDR.match(str(account)):
            raise NftAccountError(f"account {account!r} is not a 0x address")
        try:
            t_chain, t_contract, t_id = erc6551.read_token(rpc, account)
        except Exception as exc:  # noqa: BLE001
            raise NftAccountError(f"{account} did not answer token() ({exc}) — not a token-bound "
                                  f"account this build can act through") from exc
        if int(t_chain) != int(chain_id):
            raise NftAccountError(f"{account} belongs to a token on chain {t_chain}, not {chain} "
                                  f"({chain_id})")
        t_profile = by_addr.get(str(t_contract).lower())
        if t_profile is None:
            raise NftAccountError(f"{account} is the account of {t_contract} #{t_id}, which is not a "
                                  f"pinned collection on {chain}")
        if profile is not None and (t_profile.address != profile.address or int(t_id) != token_id):
            raise NftAccountError(f"account={account} is the account of {t_contract} #{t_id}, not "
                                  f"of {nft}")
        profile, token_id = t_profile, int(t_id)
    if not 1 <= int(token_id) <= int(profile.max_supply):
        raise NftAccountError(f"#{token_id} is outside the collection's ids 1..{profile.max_supply}")
    candidates = _account_of(chain_id, profile, token_id)
    if account:
        match = [a for a in candidates if a.lower() == str(account).lower()]
        if not match:
            raise NftAccountError(f"{account} is not the pinned ERC-6551 account of "
                                  f"{profile.address} #{token_id} (expected {candidates[0]})")
        acct = match[0]
    else:
        acct = candidates[0]
    why = collection_registry.runtime_refusal(rpc, profile)
    if why:
        raise NftAccountError(why)
    try:
        code = rpc("eth_getCode", [acct, "latest"])
    except Exception as exc:  # noqa: BLE001
        raise NftAccountError(f"could not read the code at the account {acct} ({exc})") from exc
    if not isinstance(code, str) or len(code) <= 2:
        raise NftAccountError(f"the account {acct} of {profile.address} #{token_id} is not deployed")
    if check_owner:
        try:
            owner = read_owner_of(rpc, profile.address, token_id)
        except Exception as exc:  # noqa: BLE001
            raise NftAccountError(f"could not read ownerOf({token_id}) on {profile.address} "
                                  f"({exc}); failing closed") from exc
        if owner.lower() != str(treasury).lower():
            raise NftAccountError(
                f"this treasury ({treasury}) does not own {profile.address} #{token_id} (its owner "
                f"is {owner}) — an agent acts through an account only as the NFT's current owner")
    return HeldNft(chain=chain, chain_id=int(chain_id), collection=profile.address,
                   token_id=int(token_id), account=acct, journal_prefix=profile.journal_prefix)


# ------------------------------------------------------------------------------ the journal

def canonical(entry: Dict[str, Any]) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(entry: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical(entry)).hexdigest()


def body_sha256(entry: Dict[str, Any]) -> str:
    """sha256 of the canonical entry without ``sig`` — what the signature commits to."""
    return hashlib.sha256(canonical({k: v for k, v in entry.items() if k != "sig"})).hexdigest()


def journal_path(home_dir, user_id: str, instance_id: Optional[str], chain_id: int,
                 account: str) -> Path:
    from core.instance import DEFAULT_INSTANCE_ID, self_tier_root
    root = Path(self_tier_root(home_dir, user_id, instance_id or DEFAULT_INSTANCE_ID))
    return root.joinpath(*JOURNAL_DIR) / f"{int(chain_id)}-{str(account).lower()}.jsonl"


def load_journal(path: Path) -> List[Dict[str, Any]]:
    """Every entry, in order. No file = ``[]``; an unreadable line RAISES (never "empty")."""
    p = Path(path)
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_entry(*, prior: Sequence[Dict[str, Any]], account: str, chain_id: int, kind: str,
                text: str, owner: str, refs: Sequence[str] = (),
                positions_after: Optional[dict] = None, ts: Optional[str] = None) -> Dict[str, Any]:
    from core.wallet.account_journal import JOURNAL_KINDS
    if kind not in JOURNAL_KINDS:
        raise ValueError(f"unknown journal kind {kind!r}")
    return {
        "seq": len(prior),
        "ts": ts or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "account": str(account).lower(), "chain_id": int(chain_id), "kind": kind,
        "text": str(text), "refs": list(refs), "positions_after": positions_after or {},
        "owner": str(owner).lower(), "prev": digest(prior[-1]) if prior else GENESIS,
    }


def sign_entry(entry: Dict[str, Any], signer, prefix: Optional[str] = None) -> Dict[str, Any]:
    from core.wallet.account_journal import DEFAULT_JOURNAL_PREFIX, journal_payload
    if str(signer.address).lower() != entry["owner"]:
        raise ValueError("the signing key is not the owner this entry names")
    payload = journal_payload(account=entry["account"], chain=entry["chain_id"], seq=entry["seq"],
                              kind=entry["kind"], text_sha256=body_sha256(entry), prev=entry["prev"],
                              prefix=prefix or DEFAULT_JOURNAL_PREFIX)
    out = dict(entry)
    out["sig"] = signer.sign_message(payload)
    return out


def append_journal(held: HeldNft, signer, *, kind: str, text: str, refs: Sequence[str] = (),
                   home_dir=None, user_id: Optional[str] = None,
                   instance_id: Optional[str] = None) -> Tuple[Dict[str, Any], Path]:
    """Sign and append one entry to *held*'s journal; returns ``(entry, path)``. Raises on any
    failure (the caller reports it — a transaction that landed is never undone by its journal)."""
    from core.instance import resolve_instance_id, resolve_owner_user_id
    from core.runtime_paths import resolve_data_home
    home_dir = home_dir if home_dir is not None else resolve_data_home()
    user_id = user_id or resolve_owner_user_id()
    instance_id = instance_id or resolve_instance_id()
    path = journal_path(home_dir, user_id, instance_id, held.chain_id, held.account)
    prior = load_journal(path)
    entry = sign_entry(build_entry(prior=prior, account=held.account, chain_id=held.chain_id,
                                   kind=kind, text=text, owner=signer.address, refs=refs),
                       signer, prefix=held.journal_prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(canonical(entry).decode("utf-8") + "\n")
    return entry, path


#: how long a verb waits for the optional journal publisher before it reports and moves on
PUBLISH_TIMEOUT_S = 8.0


def publish_after_append(chain_id: int, account: str, *, timeout: Optional[float] = None) -> str:
    """Best effort: hand the account's journal to the optional ``polyrob_drop`` publisher AFTER an
    entry was appended (so after the transaction landed); return its one status line, or ``""``
    when the package is not installed. Never raises, never waits longer than *timeout*: the
    publisher runs on a daemon thread, and a slow or failing one changes nothing but this line.
    Not a gate — nothing here authorizes or refuses; the package has its own off switch
    (``POLYROB_DROP_PUBLISH=0``)."""
    try:
        import importlib
        mod = importlib.import_module("polyrob_drop.publish")
        fn = getattr(mod, "publish_journal")
    except Exception:  # noqa: BLE001 — not installed (or broken): no publish, no noise
        return ""
    import threading
    timeout = PUBLISH_TIMEOUT_S if timeout is None else float(timeout)
    box: Dict[str, Any] = {}

    def _run() -> None:
        try:
            box["res"] = fn(int(chain_id), str(account).lower())
        except BaseException as exc:  # noqa: BLE001 — a raising publisher is only reported
            box["exc"] = exc

    worker = threading.Thread(target=_run, name="journal-publish", daemon=True)
    try:
        worker.start()
        worker.join(timeout)
    except Exception as exc:  # noqa: BLE001
        return f"journal publish: failed — {type(exc).__name__}: {exc}"
    if worker.is_alive():
        return f"journal publish: pending — no answer within {timeout:g}s; it continues in the background"
    if "exc" in box:
        exc = box["exc"]
        return f"journal publish: failed — {type(exc).__name__}: {exc}"
    res = box.get("res")
    try:
        line = res.line() if hasattr(res, "line") else str(res)
        return " ".join(str(line).split())[:300]
    except Exception as exc:  # noqa: BLE001
        return f"journal publish: failed — {type(exc).__name__}: {exc}"


def recover_owner(entry: Dict[str, Any], prefix: Optional[str] = None) -> Optional[str]:
    """The address that signed *entry* (None when the signature does not recover)."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct

        from core.wallet.account_journal import DEFAULT_JOURNAL_PREFIX, JOURNAL_TEMPLATE
        payload = JOURNAL_TEMPLATE.format(
            prefix=prefix or DEFAULT_JOURNAL_PREFIX, account=str(entry["account"]).lower(),
            chain=int(entry["chain_id"]), seq=int(entry["seq"]), kind=entry["kind"],
            text_sha256=body_sha256(entry), prev=entry["prev"]).encode("utf-8")
        return Account.recover_message(encode_defunct(payload), signature=entry["sig"]).lower()
    except Exception:  # noqa: BLE001
        return None
