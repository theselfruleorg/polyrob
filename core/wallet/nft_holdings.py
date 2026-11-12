"""The holdings watch — which NFTs of pinned collections this treasury owns (069 v4 A4).

069 v4 §3: "each tick the agent checks it still owns each NFT it tracks; a lost NFT (sold, given,
moved) is reported to its owner and dropped from its work". And the other direction: an NFT
that ARRIVES on the treasury from a pinned collection is detected and reported ("you gave me
<label> #N").

Deterministic, no model turn: an ``IntervalTicker`` on the autonomy runtime, in the
``bridge_watcher`` pattern (reads only, signs nothing; deliberately NOT pause-gated — a paused
owner still needs to know an NFT left). On each pass, per pinned collection:

* scan ``Transfer(*, treasury, id)`` logs of the collection from the last scanned block (first
  pass: the profile's ``deploy_block``) to the head — candidates that may have ARRIVED;
* read ``ownerOf`` for every candidate and every tracked id — the state is the chain's answer,
  never a log's;
* tracked and still owned: kept; tracked and no longer owned: LOST (reported once, dropped);
  owned and not tracked: ARRIVED (reported once, tracked). An ``ownerOf`` read that fails is
  UNKNOWN: the id stays tracked and nothing is reported — an unreadable answer is not a loss;
* 115 W7, an account kind with a sale-ready view: ``saleReadyOf`` per held id — READY FOR SALE
  and READINESS LOST (no transfer) are reported once each; an NFT that LEAVES while ready is
  reported as sold or moved, with its transfer's receipt facts (the pinned market's ``Sold``
  price only when that event is in the receipt; never a guessed price);
* 111 W9a: an NFT of a collection with a pinned market that leaves by a ``Sold`` of that market
  (a listing the treasury signed was filled, or an offer accepted) is reported SOLD with the
  price from the event; the open market orders (``core/wallet/market_orders.py``) are re-read
  each pass and an order that ended (expired, cancelled) is reported once and dropped.

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
#: C8: consecutive failed passes before the owner is told the watch is blind (6 × 5 min).
FAILED_PASSES_ALERT = 6
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
    #: 115 W7: a held NFT whose account kind has a sale-ready view became ready for sale, or
    #: lost its readiness with no transfer (an act through its account, or cancelReady).
    ready: List[Dict[str, Any]] = field(default_factory=list)
    unready: List[Dict[str, Any]] = field(default_factory=list)

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
    from core.wallet import collection_registry
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
                _readiness(rpc, p, tracked_rows[key], result)
                _identity(rpc, p, tracked_rows[key])
            elif owned:
                account, _version = p.account_of(token_id)
                row = {"chain": chain, "chain_id": p.chain_id, "collection": p.address,
                       "token_id": token_id, "account": account, "label": label,
                       "since": float(now())}
                tracked_rows[key] = row
                result.arrived.append(row)
                _readiness(rpc, p, row, None)
                _identity(rpc, p, row)
            elif key in tracked_rows:
                row = dict(tracked_rows.pop(key))
                row.update({"lost_at": float(now()), "new_owner": owner})
                if row.get("sale_ready"):
                    row["left_while_ready"] = _departure(rpc, p, treasury, token_id, start, head)
                elif p.erc6551p is not None:   # 111 W9a: a filled listing or an accepted offer
                    row["left"] = _departure(rpc, p, treasury, token_id, start, head)
                state.setdefault("lost", []).append(row)
                state["lost"] = state["lost"][-LOST_KEEP:]
                result.lost.append(row)
        if complete:
            scanned[ckey] = head + 1
    return result


def _readiness(rpc: Rpc, profile, row: Dict[str, Any], result: Optional[WatchResult]) -> None:
    """115 W7: read ``saleReadyOf`` for a held NFT whose account kind names the view; record it
    on the row and, with *result*, report a change (ready / readiness lost with no transfer). An
    unreadable read changes nothing and reports nothing (never "not ready")."""
    from core.wallet import sale_acts
    version = profile.version_for_account(int(row["token_id"]), row["account"])
    if version is None or not version.kind.sale_ready_view:
        return
    try:
        ready = sale_acts.read_sale_ready(rpc, profile.address, int(row["token_id"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("nft holdings watch: %s readiness unread (%s)", row.get("label"), exc)
        return
    before = row.get("sale_ready")
    row["sale_ready"] = ready
    if result is None or ready == bool(before):
        return
    (result.ready if ready else result.unready).append(row)


def _identity(rpc: Rpc, profile, row: Dict[str, Any]) -> None:
    """115 W6: the ERC-8004 identity a held NFT's account owns, for a kind whose collection names
    identities (ERC-6551P: ``hasIdentity`` / ``agentIdOf``). The agentId is read once (the
    collection records it at mint: ``agent_id``, None = no identity); each pass reads the
    registry's ``ownerOf(agentId)`` (``identity_held``: the holder may move the identity out of
    the account). An unreadable read changes nothing (``core.wallet.held_identity`` reads the
    rows)."""
    from core.wallet import abi, erc8004, erc8004_reputation
    version = profile.version_for_account(int(row["token_id"]), row["account"])
    if version is None or not version.kind.collection_iface:
        return
    try:
        if "agent_id" not in row:
            row["agent_id"] = erc8004_reputation.agent_id_of(rpc, profile.address,
                                                             int(row["token_id"]))
        if row["agent_id"] is None:
            return
        raw = rpc("eth_call", [{"to": erc8004.resolve_identity_registry(row["chain"]),
                                "data": abi.encode_call("ownerOf", [{"type": "uint256"}],
                                                        [int(row["agent_id"])])}, "latest"])
        if not isinstance(raw, str) or len(raw) < 66:
            raise ValueError(f"ownerOf({row['agent_id']}) returned {raw!r}")
        row["identity_held"] = ("0x" + raw[-40:]).lower() == str(row["account"]).lower()
    except Exception as exc:  # noqa: BLE001 — unknown is not "no identity"
        logger.warning("nft holdings watch: %s identity unread (%s)", row.get("label"), exc)


def _departure(rpc: Rpc, profile, treasury: str, token_id: int, start: int, head: int
               ) -> Dict[str, Any]:
    """The receipt facts of the transfer that took a READY NFT away: the transaction, its block,
    the recipient, and the pinned market's ``Sold`` event in the same receipt when there is one
    (the price is never guessed: a plain marketplace fill pays outside the collection)."""
    from core.wallet import sale_acts
    try:
        logs = rpc("eth_getLogs", [{"address": profile.address, "fromBlock": hex(int(start)),
                                    "toBlock": hex(int(head)),
                                    "topics": [TOPIC_TRANSFER, _word(treasury), None,
                                               "0x" + format(int(token_id), "064x")]}])
        if not isinstance(logs, list) or not logs:
            return {"error": f"no Transfer of #{token_id} from the treasury in blocks "
                             f"{start}..{head}"}
        last = logs[-1]
        out: Dict[str, Any] = {"tx": last.get("transactionHash"),
                               "block": int(str(last.get("blockNumber") or "0x0"), 16),
                               "to": "0x" + str(last["topics"][2])[-40:], "sold": None}
        market = profile.erc6551p.market if profile.erc6551p is not None else None
        if market and out["tx"]:
            receipt = rpc("eth_getTransactionReceipt", [out["tx"]]) or {}
            for lg in receipt.get("logs") or ():
                topics = [str(t).lower() for t in (lg.get("topics") or [])]
                data = str(lg.get("data") or "0x").lower().removeprefix("0x")
                if (str(lg.get("address") or "").lower() == market and len(topics) == 4
                        and topics[0] == sale_acts.TOPIC_SOLD and int(topics[2], 16) == int(token_id)
                        and len(data) == 4 * 64):
                    out["sold"] = {"buyer": "0x" + data[24:64],
                                   "currency": "0x" + data[64 + 24:128],
                                   "price": int(data[128:192], 16),
                                   "royalty": int(data[192:256], 16)}
        return out
    except Exception as exc:  # noqa: BLE001 — the facts are a courtesy; the loss stands
        return {"error": f"{type(exc).__name__}: {exc}"}


def _row_key(row: Dict[str, Any]) -> str:
    return f"{row['chain_id']}:{row['collection']}:{row['token_id']}"


def _seed_tokens(state: Dict[str, Any], rpc_for: Callable[[str], Rpc], user_id: str) -> Dict[str, str]:
    """Record what each tracked account already holds (``core.wallet.nft_inherited``), once per
    NFT: a row is marked ``tokens_seeded`` only after a COMPLETE pass, so an unreadable balance is
    tried again on the next pass. Returns one sentence per row for its arrival notice."""
    from core.wallet import nft_inherited
    said: Dict[str, str] = {}
    for key, row in (state.get("tracked") or {}).items():
        if row.get("tokens_seeded"):
            continue
        try:
            held, unread = nft_inherited.seed(row, rpc=rpc_for(row["chain"]), user_id=user_id)
        except Exception as exc:  # noqa: BLE001 — never fail the watch for the book
            held, unread = [], [f"{type(exc).__name__}: {exc}"]
        if unread:
            # Not a failed PASS (the ownership answer is good): the blind-watch alert stays quiet.
            logger.warning("nft holdings watch: %s token balances unread (%s)", row.get("label"),
                           "; ".join(unread))
            said[key] = (" I could not read all of its token balances yet ("
                         + "; ".join(unread) + "); I will try again.")
            continue
        row["tokens_seeded"] = True
        said[key] = (f" It already holds {', '.join(held)}: recorded as inherited, with no cost "
                     f"basis." if held else "")
    return said


def arrived_text(row: Dict[str, Any]) -> str:
    return (f"You gave me {row['label']} on {row['chain']}. Its account is {row['account']}; I can "
            f"now work from it as its owner. /nft lists what I hold; /nft send {row['token_id']} "
            f"<address> gives it away (clear its approvals first).")


def lost_text(row: Dict[str, Any]) -> str:
    facts = row.get("left_while_ready")
    if facts is not None:
        if facts.get("error"):
            how = f"The transfer could not be read ({facts['error']})."
        else:
            sold = facts.get("sold")
            currency = (sold or {}).get("currency") or ""
            paid = ((f"the pinned market's Sold event in it names the price {sold['price']} "
                     f"{'wei (native)' if int(currency, 16) == 0 else f'(raw units of {currency})'}, "
                     f"royalty {sold['royalty']}, buyer {sold['buyer']}.")
                    if sold else "no Sold event of the pinned market is in it, so I do not "
                                 "know the price (a plain marketplace fill pays outside the "
                                 "collection).")
            how = f"It left in tx {facts['tx']} (block {facts['block']}); {paid}"
        return (f"{row['label']} on {row['chain']} was SOLD OR MOVED while it was ready for sale: "
                f"its owner is now {row['new_owner']}. {how} Its account {row['account']} went "
                f"with it. I dropped it from my work and can no longer act from that account.")
    sold = (row.get("left") or {}).get("sold")
    if sold:
        currency = sold.get("currency") or ""
        unit = "wei (native)" if int(currency, 16) == 0 else f"(raw units of {currency})"
        return (f"{row['label']} on {row['chain']} was SOLD on its pinned market: the market's "
                f"Sold event in tx {row['left'].get('tx')} names the price {sold['price']} {unit}, "
                f"royalty {sold['royalty']}, buyer {sold['buyer']}. Its account {row['account']} "
                f"went with it. I dropped it from my work and can no longer act from that account.")
    return (f"{row['label']} on {row['chain']} is no longer mine: its owner is now "
            f"{row['new_owner']}. Its account {row['account']} went with it. I dropped it from "
            f"my work and can no longer act from that account.")


def ready_text(row: Dict[str, Any]) -> str:
    return (f"{row['label']} on {row['chain']} is READY FOR SALE: a plain marketplace fill may now "
            f"move it and everything its account {row['account']} holds. I do not act through "
            f"that account on my own while it is ready (any act ends the readiness); "
            f"agent_nft_sale_unready ends it.")


def unready_text(row: Dict[str, Any]) -> str:
    return (f"{row['label']} on {row['chain']} is no longer ready for sale (readiness lost, no "
            f"transfer): an act through its account or cancelReady ended it. A plain "
            f"marketplace fill of it now reverts.")


#: C8: per state file, ``{"failed": n, "alerted": bool, "last": str}`` for this process.
_HEALTH: Dict[str, Dict[str, Any]] = {}


def blind_text(passes: int, last: str) -> str:
    return (f"I could not check which agent NFTs I own for {passes} passes in a row "
            f"(~{passes * INTERVAL_SEC // 60} min). Last problem: {last}. Until this clears, an NFT "
            f"that arrives or leaves is not reported.")


def recovered_text(passes: int) -> str:
    return f"The agent-NFT holdings watch works again after {passes} failed passes."


async def tick(container: Any = None, *, treasury: Optional[str] = None,
               rpc_for: Optional[Callable[[str], Rpc]] = None,
               notify: Optional[Callable] = None, home_dir=None, user_id: Optional[str] = None,
               instance_id: Optional[str] = None, now: Callable[[], float] = time.time) -> WatchResult:
    """One pass: scan (off the event loop, C7), tell the owner once per change, then persist.
    Never raises. C8: after :data:`FAILED_PASSES_ALERT` failed passes in a row the owner is told
    once that the watch is blind, and once when it works again."""
    import asyncio
    result = WatchResult()
    notifier = notify or _default_notify
    health_key = None
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
        health_key = str(path)
        try:
            state = load_state(path)
        except Exception as exc:  # noqa: BLE001 — unreadable is not empty: do not rebuild over it
            logger.warning("nft holdings watch: %s is unreadable (%s) — skipping", path, exc)
            result.errors.append(f"state unreadable: {exc}")
            return result
        result = await asyncio.to_thread(scan, treasury, state=state, rpc_for=rpc_for, now=now)
        seeded = await asyncio.to_thread(_seed_tokens, state, rpc_for, user_id)
        ended = await asyncio.to_thread(_prune_orders, rpc_for, home_dir, user_id, instance_id,
                                        result)
        if result.arrived or result.lost or result.ready or result.unready:
            for row in result.arrived:
                await notifier(container, user_id, arrived_text(row) + seeded.get(_row_key(row), ""))
            for row in result.lost:
                await notifier(container, user_id, lost_text(row))
            for row in result.ready:
                await notifier(container, user_id, ready_text(row))
            for row in result.unready:
                await notifier(container, user_id, unready_text(row))
        from core.wallet import market_orders
        for row, how, why in ended:
            if how != market_orders.MOVED:   # a sale or a move is the holdings notice above
                await notifier(container, user_id, market_orders.ended_text(row, how, why))
        save_state(path, state)
    except Exception as exc:  # noqa: BLE001
        logger.warning("nft holdings watch: pass failed", exc_info=True)
        result.errors.append(f"the pass failed ({type(exc).__name__}: {exc})")
    finally:
        if health_key is not None:
            await _track_health(health_key, result, container, user_id, notifier)
    return result


def _prune_orders(rpc_for, home_dir, user_id, instance_id, result: WatchResult) -> list:
    """111 W9a: drop the market orders that ended; an unreadable orders file is an error of the
    pass (named), never "no orders"."""
    from core.wallet import market_orders
    try:
        return market_orders.prune(rpc_for, at=market_orders.path(home_dir, user_id, instance_id))
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"the market orders could not be read ({exc})")
        return []


async def _track_health(key: str, result: WatchResult, container, user_id, notifier) -> None:
    h = _HEALTH.setdefault(key, {"failed": 0, "alerted": False, "last": ""})
    try:
        if result.errors:
            h["failed"] += 1
            h["last"] = result.errors[-1]
            if h["failed"] >= FAILED_PASSES_ALERT and not h["alerted"]:
                await notifier(container, user_id, blind_text(h["failed"], h["last"]))
                h["alerted"] = True
            return
        if h["alerted"]:
            await notifier(container, user_id, recovered_text(h["failed"]))
        h.update(failed=0, alerted=False, last="")
    except Exception:  # noqa: BLE001 — the health notice is best effort
        logger.warning("nft holdings watch: health notice failed", exc_info=True)


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


def _order_lines(r: Dict[str, Any], rpc_for, home_dir, user_id, instance_id) -> List[str]:
    """The open market orders of one held NFT, each re-read live (111 §8.3 "open orders")."""
    from core.wallet import market_orders
    try:
        rows = [o for o in market_orders.open_orders(
            at=market_orders.path(home_dir, user_id, instance_id))
            if int(o.get("chain_id") or 0) == int(r["chain_id"])
            and str(o.get("collection")) == r["collection"]
            and int(o.get("token_id") or -1) == int(r["token_id"])]
    except Exception as exc:  # noqa: BLE001 — unreadable is never "none"
        return [f"  orders:  UNREADABLE ({exc}) — never read as none"]
    if not rows:
        return ["  orders:  no open market order signed by me"]
    out = [f"  orders:  {len(rows)} open market order(s) — a listing binds transferCount, not "
           f"readiness: I keep working while listed, but a spend below the listing's promise "
           f"makes its fill revert"]
    for o in rows[:5]:
        try:
            state, why = market_orders.status(rpc_for(o["chain"]), o)
        except Exception as exc:  # noqa: BLE001
            state, why = market_orders.UNKNOWN, str(exc)
        out.append("    " + market_orders.line(o, state, why))
    return out


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
    for row in res.ready:
        lines.append(f"  READY: {ready_text(row)}")
    for row in res.unready:
        lines.append(f"  READINESS LOST: {unready_text(row)}")
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
        if "sale_ready" in r:
            lines.append("  sale:    READY — a plain marketplace fill may move it; I hold my own "
                         "acts through the account" if r["sale_ready"] else "  sale:    not ready")
        lines.extend(_order_lines(r, rpc_for, home_dir, user_id, instance_id))
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
            version = profile.version_for_account(int(r["token_id"]), r["account"])
            kind = version.kind if version is not None else None
            if approvals:
                lines.append(f"  approvals: {len(approvals)} OPEN ({cover}) — it cannot be sent until "
                             f"they are cleared (agent_nft_revoke_all)")
                if kind is not None and kind.found_approval_text:
                    lines.append(f"    ⚠ {kind.found_approval_text}")
                for a in approvals[:5]:
                    lines.append(f"    {a.kind} {a.contract}"
                                 + (f" #{a.token_id}" if a.token_id is not None else "")
                                 + f" → {a.spender}" + ("" if a.verified else " (unconfirmed)"))
            elif kind is not None and kind.approvals_text:
                lines.append(f"  approvals: none — {kind.approvals_text} ({cover}; "
                             f"{kind.residuals_text})")
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
