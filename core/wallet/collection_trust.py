"""The owner's collection trust, from any owner seat (``/nft trust``).

Before this, the only way to pin an agent-NFT collection was a root-owned file
(``/etc/polyrob/agent_nft_collections.json``) assembled by hand: a shell, ``sudo`` and a
JSON blob the owner could not check. Approving or revoking a token took one tap. This module
is the chat seat for the same decision, built the way ``core.wallet.token_trust`` is:

* ``trust_quote`` — READS the contract (code hash, ERC-721, supply, deploy block, how many
  the treasury holds) and prints the facts with the confirm line;
* ``trust`` (``… go``) — re-reads everything (nothing from the quote is reused) and writes a
  profile with NO capabilities to the owner's pin store
  (``collection_registry.owner_pins_path``). An owner pin lets the agent see and act
  through the accounts of NFTs it owns; it never arms a collection mint or reveal, which
  stay root-file pins (``capabilities``);
* ``untrust`` — removes an owner pin. A root-file pin stays the system admin's.

⚠️ No agent tool calls this. Every writer refuses unless the context is a genuine owner turn
(``core.security.owner_turn``), and the agent's file tools are denied the store
(``core/security/secret_guard.py``). A pin the agent could write would let it vouch for a
contract itself.

The pinned code hash is re-checked against the live code before every act, exactly as for a
root-file pin (``collection_registry.runtime_refusal``).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

Rpc = Callable[..., Any]

#: ERC-165 interface id of ERC-721.
_ERC721_IID = "80ac58cd"



def _refusal(ctx: Any, verb: str) -> Optional[str]:
    from core.security.owner_turn import owner_turn_refusal
    return owner_turn_refusal(ctx, verb=verb, does="decides which collections are trusted",
                              public="decide which collections are trusted")


def _call(rpc: Rpc, to: str, data: str, block: str = "latest") -> str:
    return rpc("eth_call", [{"to": to, "data": data}, block])


def _uint_view(rpc: Rpc, to: str, signature: str) -> Optional[int]:
    from core.wallet import abi
    try:
        raw = _call(rpc, to, abi.selector(signature))
        return int(abi.decode([{"type": "uint256"}], raw)[0])
    except Exception:  # noqa: BLE001 — absent view
        return None


def _string_view(rpc: Rpc, to: str, signature: str) -> str:
    from core.wallet import abi
    try:
        return str(abi.decode([{"type": "string"}], _call(rpc, to, abi.selector(signature)))[0])
    except Exception:  # noqa: BLE001
        return ""


def _is_erc721(rpc: Rpc, address: str) -> bool:
    from core.wallet import abi
    try:
        raw = _call(rpc, address, abi.selector("supportsInterface(bytes4)")
                    + _ERC721_IID.ljust(64, "0"))
        return bool(abi.decode([{"type": "bool"}], raw)[0])
    except Exception:  # noqa: BLE001
        return False


def _has_code(rpc: Rpc, address: str, block: int) -> bool:
    code = rpc("eth_getCode", [address, hex(block)])
    return isinstance(code, str) and len(code) > 2


def _deploy_block(rpc: Rpc, address: str, head: int) -> int:
    """The first block with code at *address* (binary search). Raises when the node cannot
    answer a historical read (not an archive node)."""
    lo, hi = 0, head
    while lo < hi:
        mid = (lo + hi) // 2
        if _has_code(rpc, address, mid):
            hi = mid
        else:
            lo = mid + 1
    # A pruned node may answer "0x" for old state instead of an error, which pushes the search
    # late and makes the watch miss earlier arrivals. The block before the deploy must answer
    # "0x" AND the block must hold code; probe the genesis side too: a node that serves block
    # 1's state honestly serves any.
    if not _has_code(rpc, address, lo):
        raise RuntimeError(f"no code at block {lo}")
    if lo > 0:
        rpc("eth_getBalance", [address, hex(1)])  # raises on a node without old state
    return lo


def _rpc_for(chain: str) -> Rpc:
    from core.wallet.simulation import _default_rpc_for
    return _default_rpc_for(chain)


def _treasury() -> Optional[str]:
    try:
        from core.wallet.factory import get_agent_wallet
        w = get_agent_wallet()
        return w.operational_signer().address if w is not None else None
    except Exception:  # noqa: BLE001
        return None


def read_facts(chain: str, address: str, *, rpc: Optional[Rpc] = None,
               max_supply: Optional[int] = None, from_block: Optional[int] = None,
               treasury: Optional[str] = None) -> Dict[str, Any]:
    """Everything a profile needs, read from the chain now. Raises ``ValueError`` with the
    owner's remedy when a fact cannot be read."""
    from core.wallet import collection_registry, erc6551
    from core.wallet.addresses import normalize_for_chain
    chain = str(chain or "").strip().lower()
    chain_id = collection_registry.chain_id_of(chain)
    if not chain_id:
        raise ValueError(f"{chain!r} is not an EVM chain I know")
    try:
        address = normalize_for_chain(chain, address)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{address!r} is not an address on {chain} ({exc})") from exc
    rpc = rpc or _rpc_for(chain)
    try:
        runtime = collection_registry.runtime_sha256_of(rpc, address)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"I could not read the contract code at {address} on {chain} ({exc})") from exc
    if not _is_erc721(rpc, address):
        raise ValueError(f"{address} on {chain} does not say it is an ERC-721 collection "
                         f"(supportsInterface 0x{_ERC721_IID})")
    supply = max_supply or _uint_view(rpc, address, "maxSupply()") \
        or _uint_view(rpc, address, "MAX_SUPPLY()")
    if not supply:
        raise ValueError(f"{address} has no maxSupply() I can read. Name it: "
                         f"/nft trust {address} on {chain} max <n>")
    if int(supply) > collection_registry.MAX_OWNER_SUPPLY:
        raise ValueError(f"its supply cap {supply} is above the {collection_registry.MAX_OWNER_SUPPLY} a chat pin "
                         f"can carry (the guard checks every id's account). The system admin "
                         f"can pin it in {collection_registry.REGISTRY_FILE}")
    head = int(rpc("eth_blockNumber", []), 16)
    if from_block is not None and not 0 <= int(from_block) <= head:
        raise ValueError(f"`from {from_block}` is not a block on {chain} (the head is {head})")
    if from_block is None:
        try:
            from_block = _deploy_block(rpc, address, head)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"I could not find the block that deployed {address} ({exc}; the "
                             f"RPC may not keep old state). Name it: "
                             f"/nft trust {address} on {chain} from <block>") from exc
    held = None
    treasury = treasury or _treasury()
    if treasury:
        from core.wallet import abi
        try:
            held = int(abi.decode([{"type": "uint256"}], _call(rpc, address, abi.encode_call(
                "balanceOf", [{"type": "address"}], [treasury])))[0])
        except Exception:  # noqa: BLE001
            held = None
    return {"chain": chain, "chain_id": chain_id, "address": address,
            "name": _string_view(rpc, address, "name()"),
            "symbol": _string_view(rpc, address, "symbol()"),
            "runtime_sha256": runtime, "max_supply": int(supply),
            "deploy_block": int(from_block), "held": held, "treasury": treasury,
            "profile": {"spec": collection_registry.SPEC_V1, "capabilities": [],
                        "chain_id": chain_id, "address": address, "runtime_sha256": runtime,
                        "deploy_block": int(from_block), "max_supply": int(supply),
                        "accounts": [{"registry": erc6551.REGISTRY,
                                      "implementation": erc6551.ACCOUNT_V3_IMPL,
                                      "salt": 0}]}}


def _pinned(chain_id: int, address: str) -> Optional[str]:
    """``"admin"`` / ``"owner"`` when already pinned, else None. Raises on an untrusted store."""
    from core.wallet import collection_registry
    a = address.lower()
    for p, src in collection_registry.pinned_with_source():
        if p.chain_id == chain_id and p.address == a:
            return src
    return None


def _extras(max_supply: Optional[int], from_block: Optional[int]) -> str:
    return ((f" max {max_supply}" if max_supply else "")
            + (f" from {from_block}" if from_block is not None else ""))


def _render(f: Dict[str, Any]) -> str:
    title = " ".join(x for x in (f["name"], f"({f['symbol']})" if f["symbol"] else "") if x)
    lines = [f"Collection {title or f['address']} on {f['chain']}",
             f"  contract:   {f['address']}",
             f"  code hash:  {f['runtime_sha256'][:16]}… (checked again before every act)",
             f"  supply cap: {f['max_supply']}",
             f"  deployed:   block {f['deploy_block']}"]
    if f["held"] is not None:
        lines.append(f"  I hold:     {f['held']} of them")
    return "\n".join(lines)


def trust_quote(chain: str, address: str, *, rpc: Optional[Rpc] = None,
                max_supply: Optional[int] = None, from_block: Optional[int] = None,
                confirm_line: Optional[str] = None) -> str:
    """The facts and the confirm line. Reads only. *confirm_line* is the owner's own words
    without ``go`` (a seat passes them, so the card matches what was typed)."""
    try:
        f = read_facts(chain, address, rpc=rpc, max_supply=max_supply, from_block=from_block)
        src = _pinned(f["chain_id"], f["address"])
    except Exception as exc:  # noqa: BLE001
        return f"❌ Not trusted: {exc}"
    if src:
        return f"{_render(f)}\n\nAlready trusted ({'system admin file' if src == 'admin' else 'by you'})."
    return (f"{_render(f)}\n\nTrusting it lets me see the NFTs of this collection that I own and "
            f"act from their accounts (every money move still passes the guard and your caps). "
            f"It does not let me mint or reveal.\n\n"
            f"Add `go` to trust it: "
            f"{confirm_line or f'/nft trust {address} on {chain}' + _extras(max_supply, from_block)} go")


def _write(path: str, raw_profiles) -> None:
    from core.wallet import collection_registry
    doc = {"profiles": list(raw_profiles)}
    collection_registry.parse(doc)  # never write what the reader would refuse
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, path)


def _read_raw(path: str):
    """The stored rows, validated exactly as the reader validates them (raises otherwise)."""
    from core.wallet import collection_registry
    if not os.path.exists(path):
        return []
    collection_registry.load_owner_pins(path)
    with open(path, "r", encoding="utf-8") as fh:
        return list(json.load(fh)["profiles"])


class _Locked:
    """One writer at a time: a read-modify-write without it can drop a concurrent pin."""

    def __init__(self, path: str):
        self.path = path + ".lock"

    def __enter__(self):
        import fcntl
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.fh.close()


def _signer_note() -> str:
    if (os.environ.get("WALLET_SIGNER") or "").strip().lower() == "remote":
        return ("\n⚠️ WALLET_SIGNER=remote: the signer reads its own data home. If it cannot read "
                "this pin, it refuses acts from these accounts — then the system admin pins the "
                "collection in /etc/polyrob/agent_nft_collections.json.")
    return ""


def trust(ctx: Any, chain: str, address: str, *, rpc: Optional[Rpc] = None,
          max_supply: Optional[int] = None, from_block: Optional[int] = None,
          path: Optional[str] = None) -> Tuple[bool, str]:
    """Pin the collection on the owner's word. Re-reads every fact now."""
    refusal = _refusal(ctx, "trust collection")
    if refusal:
        return False, refusal
    from core.wallet import collection_registry
    path = path or collection_registry.owner_pins_path()
    try:
        f = read_facts(chain, address, rpc=rpc, max_supply=max_supply, from_block=from_block)
        src = _pinned(f["chain_id"], f["address"])
        if src:
            return True, f"Already trusted ({'system admin file' if src == 'admin' else 'by you'})."
        with _Locked(path):
            _write(path, _read_raw(path) + [f["profile"]])
    except Exception as exc:  # noqa: BLE001
        logger.warning("collection trust failed", exc_info=True)
        fix = (" If the store of your pins is broken: /nft untrust all go"
               if isinstance(exc, collection_registry.CollectionRegistryError) else "")
        return False, f"❌ Not trusted: {exc}. Nothing was written.{fix}"
    logger.info("owner trusted collection %s on %s", f["address"], f["chain"])
    return True, (f"✅ Trusted.\n{_render(f)}\n\n"
                  f"I watch it now: an NFT of it that reaches my address is reported to you, "
                  f"and /nft lists it with its account.{_signer_note()}")


def untrust(ctx: Any, chain: str, address: str, *, path: Optional[str] = None) -> Tuple[bool, str]:
    """Remove the owner's pin. A root-file pin is not the owner seat's to remove."""
    refusal = _refusal(ctx, "untrust collection")
    if refusal:
        return False, refusal
    from core.wallet import collection_registry
    path = path or collection_registry.owner_pins_path()
    if str(address or "").strip().lower() == "all":
        return _reset(path)
    chain_id = collection_registry.chain_id_of(str(chain or "").strip().lower())
    a = str(address or "").strip().lower()
    try:
        with _Locked(path):
            rows = _read_raw(path)
            keep = [r for r in rows if not (int(r["chain_id"]) == chain_id
                                            and str(r["address"]).lower() == a)]
            if len(keep) < len(rows):
                _write(path, keep)
    except Exception as exc:  # noqa: BLE001
        logger.warning("collection untrust failed", exc_info=True)
        return False, (f"❌ Not changed: {exc}. To clear every pin you made: "
                       f"/nft untrust all go")
    if len(keep) == len(rows):
        try:
            admin = any(p.chain_id == chain_id and p.address == a
                        for p in collection_registry.load())
        except Exception:  # noqa: BLE001
            admin = False
        if admin:
            return False, (f"{address} is pinned by the system admin file "
                           f"{collection_registry.REGISTRY_FILE}; only root can remove it.")
        return False, f"{address} on {chain} is not a collection you trusted."
    return True, (f"✅ No longer trusted: {address} on {chain}. I stop acting from its accounts; "
                  f"an NFT of it that I hold stays where it is.")


def _reset(path: str) -> Tuple[bool, str]:
    """``/nft untrust all``: every owner pin goes. The file is moved aside, never deleted, so a
    store that could not be read (and so cannot be edited) is still evidence."""
    import time
    if not os.path.exists(path):
        return True, "You trust no collection from chat; nothing to clear."
    aside = f"{path}.cleared-{int(time.time())}"
    try:
        with _Locked(path):
            os.replace(path, aside)
    except Exception as exc:  # noqa: BLE001
        return False, f"❌ Not changed: {exc}"
    return True, (f"✅ Cleared every collection you trusted from chat (the old file is kept as "
                  f"{os.path.basename(aside)}). The system admin's pins stay.")


def status_lines() -> list:
    """The ``/nft`` header: which collections are trusted, and by whom."""
    from core.wallet import collection_registry
    from core.wallet.nft_account import chain_name_for_id
    try:
        rows = collection_registry.pinned_with_source()
    except Exception as exc:  # noqa: BLE001 — unreadable is not empty
        return [f"Trusted collections: UNREADABLE ({exc}). I act from no account it should cover "
                f"until it is fixed. To clear the pins you made: /nft untrust all go"]
    if not rows:
        return ["Trusted collections: none. Trust one first: /nft trust <collection> on <chain>"]
    out = ["Trusted collections:"]
    for p, src in rows:
        who = "system admin file" if src == "admin" else "you"
        out.append(f"  {p.address} on {chain_name_for_id(p.chain_id) or p.chain_id} ({who})")
    return out


__all__ = ["read_facts", "status_lines", "trust", "trust_quote", "untrust"]
