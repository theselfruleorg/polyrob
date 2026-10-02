"""The holdings watch — which NFTs of pinned collections this treasury owns (069 v4 A4).

069 v4 §3: "each tick the agent checks it still owns each NFT it tracks; a lost NFT (sold, given,
moved) is reported to its owner and dropped from its work". And the other direction: an NFT
that ARRIVES on the treasury from a pinned collection is detected and reported ("you gave me
POLYROB #N").

Deterministic, no model turn: an ``IntervalTicker`` on the autonomy runtime, in the
``bridge_watcher`` pattern (reads only, signs nothing; deliberately NOT pause-gated — a paused
owner still needs to know an NFT left). On each pass, per pinned collection:

* scan ``Transfer(*, treasury, id)`` logs of the collection from the last scanned block (first
  pass: the profile's ``deploy_block``) to the head — candidates that may have ARRIVED;
* read ``ownerOf`` for every candidate and every tracked id — the state is the chain's answer,
  never a log's;
* tracked and still owned: kept; tracked and no longer owned: LOST (reported once, dropped);
  owned and not tracked: ARRIVED (reported once, tracked). An ``ownerOf`` read that fails is
  UNKNOWN: the id stays tracked and nothing is reported — an unreadable answer is not a loss.

State: ``<self tier>/agent_nft/holdings.json`` (``tracked``, ``scanned`` per collection, the
last ``lost`` rows). The state changes only after the notice was handed to the owner rail, so a
crash between the two repeats one notice rather than losing it. Gate: ``AGENT_NFT_ENABLED``
(default OFF) — no flag of its own.

"Dropped from its work": the tracked set is what :func:`select` (the default NFT of the
``agent_nft`` verbs) and ``/nft`` read, and every call through an account re-reads the owner
anyway (``core.wallet.nft_account.resolve`` and ``tx_guard``'s pre-flight) — a lost NFT cannot
be acted through.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

INTERVAL_SEC = 300
STATE_FILE = ("agent_nft", "holdings.json")
LOG_STEP = 50_000
LOST_KEEP = 50
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

Rpc = Callable[[str, list], Any]


def enabled() -> bool:
    from core.env import bool_env
    return bool_env("AGENT_NFT_ENABLED", False)


@dataclass
class WatchResult:
    arrived: List[Dict[str, Any]] = field(default_factory=list)
    lost: List[Dict[str, Any]] = field(default_factory=list)
    kept: int = 0
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, int]:
        return {"arrived": len(self.arrived), "lost": len(self.lost), "kept": self.kept,
                "errors": len(self.errors)}


# ------------------------------------------------------------------------------ state

def state_path(home_dir=None, user_id: Optional[str] = None, instance_id: Optional[str] = None) -> Path:
    from core.instance import resolve_instance_id, resolve_owner_user_id, self_tier_root
    from core.runtime_paths import resolve_data_home
    home_dir = home_dir if home_dir is not None else resolve_data_home()
    root = Path(self_tier_root(home_dir, user_id or resolve_owner_user_id(),
                              instance_id or resolve_instance_id()))
    return root.joinpath(*STATE_FILE)


def load_state(path: Path) -> Dict[str, Any]:
    """The state; no file = empty. An unreadable file RAISES (never "holds nothing")."""
    p = Path(path)
    if not p.exists():
        return {"version": 1, "tracked": {}, "scanned": {}, "lost": []}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tracked"), dict):
        raise ValueError(f"{p} is not a holdings state file")
    data.setdefault("scanned", {})
    data.setdefault("lost", [])
    return data


def save_state(path: Path, state: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def tracked(home_dir=None, user_id=None, instance_id=None, *, chain: Optional[str] = None) -> List[Dict[str, Any]]:
    """The tracked NFTs (optionally on one chain), oldest first. Raises when unreadable."""
    rows = list(load_state(state_path(home_dir, user_id, instance_id))["tracked"].values())
    if chain:
        rows = [r for r in rows if r.get("chain") == chain]
    return sorted(rows, key=lambda r: (r.get("since") or 0, r.get("token_id") or 0))


# ------------------------------------------------------------------------------ the scan

def _word(address: str) -> str:
    return "0x" + "0" * 24 + str(address).lower().removeprefix("0x")


def _arrival_candidates(rpc: Rpc, collection: str, treasury: str, start: int, head: int) -> List[int]:
    ids: List[int] = []
    block = int(start)
    while block <= head:
        end = min(block + LOG_STEP - 1, head)
        logs = rpc("eth_getLogs", [{"address": collection, "fromBlock": hex(block),
                                     "toBlock": hex(end),
                                     "topics": [TOPIC_TRANSFER, None, _word(treasury)]}])
        if not isinstance(logs, list):
            raise RuntimeError(f"eth_getLogs {block}-{end} returned no list")
        for log in logs:
            topics = log.get("topics") or []
            if len(topics) == 4:          # ERC-721: tokenId indexed
                ids.append(int(topics[3], 16))
        block = end + 1
    return ids


def scan(treasury: str, *, state: Dict[str, Any], rpc_for: Callable[[str], Rpc],
         now: Callable[[], float] = time.time) -> WatchResult:
    """One pass over every pinned collection; mutates *state* to the new truth and returns what
    changed. Never raises for a collection read (it lands in ``errors`` and that collection's
    tracked ids stay as they were)."""
    from core.wallet import collection_registry, erc6551
    from core.wallet.nft_account import chain_name_for_id, read_owner_of

    result = WatchResult()
    try:
        profiles = collection_registry.profiles()
    except Exception as exc:  # noqa: BLE001 — an untrusted registry: change nothing
        result.errors.append(f"the collection registry cannot be trusted ({exc})")
        return result
    tracked_rows: Dict[str, Dict[str, Any]] = state.setdefault("tracked", {})
    scanned: Dict[str, int] = state.setdefault("scanned", {})
    for p in profiles:
        chain = chain_name_for_id(p.chain_id)
        ckey = f"{p.chain_id}:{p.address}"
        mine = {k: r for k, r in tracked_rows.items() if k.startswith(ckey + ":")}
        if not chain:
            result.errors.append(f"{p.address}: chain {p.chain_id} is not one this build knows")
            continue
        try:
            rpc = rpc_for(chain)
            head = int(rpc("eth_blockNumber", []), 16)
            start = int(scanned.get(ckey, p.deploy_block))
            candidates = set(_arrival_candidates(rpc, p.address, treasury, start, head)) if start <= head else set()
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{p.address} on {chain}: the scan failed ({exc})")
            continue
        candidates |= {int(r["token_id"]) for r in mine.values()}
        complete = True
        for token_id in sorted(candidates):
            key = f"{ckey}:{token_id}"
            try:
                owner = read_owner_of(rpc, p.address, token_id)
            except Exception as exc:  # noqa: BLE001 — unknown is not lost
                complete = False
                result.errors.append(f"{p.address} #{token_id}: ownerOf unreadable ({exc})")
                continue
            owned = owner.lower() == str(treasury).lower()
            label = f"{p.journal_prefix or p.address[:10] + '…'} #{token_id}"
            if owned and key in tracked_rows:
                result.kept += 1
            elif owned:
                account = erc6551.account_address(p.chain_id, p.address, token_id,
                                                  salt=p.accounts[0].salt,
                                                  implementation=p.accounts[0].implementation)
                row = {"chain": chain, "chain_id": p.chain_id, "collection": p.address,
                       "token_id": token_id, "account": account, "label": label,
                       "since": float(now())}
                tracked_rows[key] = row
                result.arrived.append(row)
            elif key in tracked_rows:
                row = dict(tracked_rows.pop(key))
                row.update({"lost_at": float(now()), "new_owner": owner})
                state.setdefault("lost", []).append(row)
                state["lost"] = state["lost"][-LOST_KEEP:]
                result.lost.append(row)
        if complete:
            scanned[ckey] = head + 1
    return result


def arrived_text(row: Dict[str, Any]) -> str:
    return (f"You gave me {row['label']} on {row['chain']}. Its account is {row['account']}; I can "
            f"now work from it as its owner. /nft lists what I hold; /nft send {row['token_id']} "
            f"<address> gives it away (clear its approvals first).")


def lost_text(row: Dict[str, Any]) -> str:
    return (f"{row['label']} on {row['chain']} is no longer mine: its owner is now "
            f"{row['new_owner']}. Its account {row['account']} went with it. I dropped it from "
            f"my work and can no longer act from that account.")


async def tick(container: Any = None, *, treasury: Optional[str] = None,
               rpc_for: Optional[Callable[[str], Rpc]] = None,
               notify: Optional[Callable] = None, home_dir=None, user_id: Optional[str] = None,
               instance_id: Optional[str] = None, now: Callable[[], float] = time.time) -> WatchResult:
    """One pass: scan, tell the owner once per change, then persist. Never raises."""
    result = WatchResult()
    try:
        if treasury is None:
            from core.wallet.factory import get_agent_wallet
            wallet = get_agent_wallet()
            if wallet is None:
                return result
            treasury = wallet.operational_signer().address
        if rpc_for is None:
            from core.wallet.simulation import _default_rpc_for
            rpc_for = _default_rpc_for
        from core.instance import resolve_owner_user_id
        user_id = user_id or resolve_owner_user_id()
        path = state_path(home_dir, user_id, instance_id)
        try:
            state = load_state(path)
        except Exception as exc:  # noqa: BLE001 — unreadable is not empty: do not rebuild over it
            logger.warning("nft holdings watch: %s is unreadable (%s) — skipping", path, exc)
            result.errors.append(f"state unreadable: {exc}")
            return result
        result = scan(treasury, state=state, rpc_for=rpc_for, now=now)
        if result.arrived or result.lost:
            notifier = notify or _default_notify
            for row in result.arrived:
                await notifier(container, user_id, arrived_text(row))
            for row in result.lost:
                await notifier(container, user_id, lost_text(row))
        save_state(path, state)
    except Exception:  # noqa: BLE001
        logger.warning("nft holdings watch: pass failed", exc_info=True)
    return result


async def _default_notify(container, user_id, text) -> None:
    from core.surfaces.user_delivery import deliver_user_message
    await deliver_user_message(container, user_id, text, source="nft_holdings")


# ------------------------------------------------------------------------------ selection

def select(chain: str, nft: Optional[str], *, rpc: Rpc, treasury: str, account: Optional[str] = None,
           home_dir=None, user_id=None, instance_id=None):
    """The NFT a verb acts on: the one named (``nft=`` / ``account=``), else the ONLY tracked NFT
    on *chain*. Always re-verified now (``nft_account.resolve``: pinned, code hash, deployed
    account, ``ownerOf == treasury``). Raises ``NftAccountError``."""
    from core.wallet.nft_account import NftAccountError, resolve
    if not nft and not account:
        try:
            rows = tracked(home_dir, user_id, instance_id, chain=chain)
        except Exception as exc:  # noqa: BLE001
            raise NftAccountError(f"the holdings record is unreadable ({exc}) — name the NFT") from exc
        if not rows:
            raise NftAccountError(f"this treasury holds no tracked NFT of a pinned collection on "
                                  f"{chain} (give it one, or name it: nft=<collection>#<id>)")
        if len(rows) > 1:
            names = ", ".join(f"{r['collection']}#{r['token_id']}" for r in rows)
            raise NftAccountError(f"this treasury holds {len(rows)} NFTs on {chain} — name one: {names}")
        nft = f"{rows[0]['collection']}#{rows[0]['token_id']}"
    return resolve(chain, rpc=rpc, treasury=treasury, account=account, nft=nft)


# ------------------------------------------------------------------------------ /nft (owner)

def _book_rows(user_id: str, account: str) -> Optional[List[Dict[str, Any]]]:
    try:
        from core import open_positions
        rows = open_positions.entries_for(user_id, account=str(account).lower(), strict=True)
    except Exception:  # noqa: BLE001 — unreadable is not empty
        return None
    return [{"symbol": e.symbol, "address": e.address, "qty": e.qty, "origin": e.origin}
            for e in rows.values()]


def overview(*, treasury: str, rpc_for: Optional[Callable[[str], Rpc]] = None, home_dir=None,
             user_id: Optional[str] = None, instance_id: Optional[str] = None,
             now: Callable[[], float] = time.time) -> str:
    """The owner's ``/nft`` view: refresh the watch (an arrival or a loss found here is reported
    HERE, once), then each owned NFT of a pinned collection with its account, the account's
    native balance, its open approvals (coverage named) and its book rows. Reads only."""
    from core.wallet import collection_registry, erc6551
    from core.instance import resolve_owner_user_id
    if rpc_for is None:
        from core.wallet.simulation import _default_rpc_for
        rpc_for = _default_rpc_for
    user_id = user_id or resolve_owner_user_id()
    path = state_path(home_dir, user_id, instance_id)
    try:
        state = load_state(path)
    except Exception as exc:  # noqa: BLE001
        return f"Agent NFTs: the holdings record {path} is unreadable ({exc}) — I cannot say what I hold."
    res = scan(treasury, state=state, rpc_for=rpc_for, now=now)
    try:
        save_state(path, state)
    except Exception:  # noqa: BLE001
        logger.warning("nft holdings: could not save %s", path, exc_info=True)
    lines = [f"Agent NFTs on my treasury {treasury}:"]
    for row in res.arrived:
        lines.append(f"  NEW: {arrived_text(row)}")
    for row in res.lost:
        lines.append(f"  GONE: {lost_text(row)}")
    for err in res.errors:
        lines.append(f"  ⚠ not checked: {err}")
    rows = sorted(state.get("tracked", {}).values(),
                  key=lambda r: (r.get("chain", ""), r.get("collection", ""), r.get("token_id", 0)))
    if not rows:
        lines.append("  none — I own no NFT of a pinned collection"
                     + (" (but some reads failed, see above)" if res.errors else ""))
        return "\n".join(lines)
    for r in rows:
        lines.append(f"\n{r['label']} — {r['collection']} #{r['token_id']} on {r['chain']}")
        lines.append(f"  account: {r['account']}")
        try:
            rpc = rpc_for(r["chain"])
            bal = int(rpc("eth_getBalance", [r["account"], "latest"]), 16)
            lines.append(f"  native:  {bal / 1e18:.6f} ({bal} wei)")
        except Exception as exc:  # noqa: BLE001
            rpc = None
            lines.append(f"  native:  UNKNOWN (read failed: {exc}) — not zero")
        try:
            profile = collection_registry.profile_for(int(r["chain_id"]), r["collection"])
            head = int(rpc("eth_blockNumber", []), 16)
            approvals = erc6551.open_approvals(rpc, r["account"], profile.deploy_block, to_block=head)
            cover = f"complete: blocks {profile.deploy_block}..{head}, {'/'.join(erc6551.APPROVAL_KINDS)}"
            if approvals:
                lines.append(f"  approvals: {len(approvals)} OPEN ({cover}) — it cannot be sent until "
                             f"they are cleared (agent_nft_revoke_all)")
                for a in approvals[:5]:
                    lines.append(f"    {a.kind} {a.contract}"
                                 + (f" #{a.token_id}" if a.token_id is not None else "")
                                 + f" → {a.spender}" + ("" if a.verified else " (unconfirmed)"))
            else:
                lines.append(f"  approvals: none ({cover})")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  approvals: INCOMPLETE — the scan failed ({exc}); never read as none")
        book = _book_rows(user_id, r["account"])
        if book is None:
            lines.append("  book:    unreadable")
        elif not book:
            lines.append("  book:    no open positions recorded for this account")
        else:
            for b in book[:8]:
                lines.append(f"  book:    {b.get('symbol') or b.get('address')} qty {b.get('qty')} "
                             f"({b.get('origin') or 'trade'})")
    return "\n".join(lines)
