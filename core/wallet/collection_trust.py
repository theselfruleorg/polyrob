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

# 124 W3: the chain-fact reads live in core/wallet/collection_registry.py (a module of the
# largest import cycle already), so the account resolve and the watch read an unpinned
# collection without pulling this module into that cycle.
from core.wallet.collection_registry import read_facts  # noqa: E402



def _refusal(ctx: Any, verb: str) -> Optional[str]:
    from core.security.owner_turn import owner_turn_refusal
    return owner_turn_refusal(ctx, verb=verb, does="decides which collections are trusted",
                              public="decide which collections are trusted")


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
    kind = f["account_kind"]
    if f.get("account_runtime_sha256"):
        lines.append(f"  accounts:   {kind.label} {f['account_impl']} (code "
                     f"{f['account_runtime_sha256'][:16]}…; ERC-165 proven: collection "
                     f"{kind.collection_iface}, account {kind.account_iface}; it signs nothing "
                     f"and holds no standing approval)")
        std = f.get("standard")
        if std:
            lines += [f"  market:     {std['market']} (code {std['market_runtime_sha256'][:16]}…)",
                      f"  zone:       {std['zone']} (code {std['zone_runtime_sha256'][:16]}…)"]
        else:
            lines.append("  sale rails: none pinned (it names no market and zone)")
    else:
        lines.append(f"  accounts:   {kind.label}")
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


__all__ = ["status_lines", "trust", "trust_quote", "untrust"]
