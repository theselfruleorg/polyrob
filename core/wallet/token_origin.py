"""Where a token came from: deployer reputation and launch forensics
(proposal 071 §3.7, W4). Read-only and keyless.

* **EVM** — the chain's keyless Blockscout v2 API: the token's creator and
  creation transaction, the deployer's other contract creations (its EARLIEST
  transactions, one page), and whether the deployer still holds the token
  (``core.wallet.onchain.token_balances``) or has sent it on.
* **Solana** — the mint's EARLIEST signature (``getSignaturesForAddress``
  paged backwards, bounded): its fee payer is the deployer; a pump.fun
  program in that transaction says the launchpad. Then launch forensics: the
  signers that received the token in the first slots, and whether several
  were funded by the same address one hop back.

⚠️ Concentration and funding are claims about ADDRESSES, not people. A shared
funder is a signal, not proof: an exchange hot wallet funds thousands of
unrelated users. Every "not read" is named; a bound that ran out is never
rendered as "nothing found".
"""
from __future__ import annotations

import concurrent.futures as _cf
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.wallet import activity as _act

logger = logging.getLogger(__name__)

PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

#: Pages of 1,000 signatures paged back toward a mint's creation.
MAX_SIG_PAGES = 5
SIG_PAGE = 1000
#: Slots after the creation slot that count as "the launch".
LAUNCH_SLOTS = 2
#: Launch transactions read for buyers, and buyers traced one hop back.
MAX_LAUNCH_TX = 10
MAX_BUYERS = 8
#: A buyer's transactions read (newest-first before its buy) to find a funder.
FUNDER_TX = 3
BUDGET_SEC = 30.0

CLUSTER_LABEL = ("addresses, not people; a shared funder is a signal, not proof "
                 "(an exchange funds many unrelated users)")


@dataclass
class Creation:
    address: str
    name: Optional[str] = None       # explorer label, attacker-written
    ref: Optional[str] = None
    time: Optional[int] = None


@dataclass
class Buyer:
    address: str
    raw: int
    ref: str
    slot: Optional[int] = None
    funder: Optional[str] = None
    funder_ref: Optional[str] = None
    funder_read: bool = False        # False = the trace did not run / failed


@dataclass
class LaunchForensics:
    first_slot: Optional[int] = None
    slots: int = LAUNCH_SLOTS
    launch_tx_read: int = 0
    buyers: List[Buyer] = field(default_factory=list)
    shared_funders: Dict[str, List[str]] = field(default_factory=dict)
    deployer_funded: List[str] = field(default_factory=list)
    not_read: List[str] = field(default_factory=list)
    label: str = CLUSTER_LABEL


@dataclass
class OriginReport:
    chain: str
    token: str
    available: bool
    source: str = ""
    reason: Optional[str] = None
    symbol: Optional[str] = None     # attacker-written
    name: Optional[str] = None       # attacker-written
    decimals: Optional[int] = None
    deployer: Optional[str] = None   # the EOA / fee payer that sent the creation
    creator_contract: Optional[str] = None   # a factory/launchpad that CREATE'd it
    creation_ref: Optional[str] = None
    creation_time: Optional[int] = None
    creation_slot: Optional[int] = None
    launchpad: Optional[str] = None
    explorer_flags: List[str] = field(default_factory=list)
    other_creations: List[Creation] = field(default_factory=list)
    other_creations_read: Optional[int] = None   # deployer txs scanned
    other_creations_more: Optional[bool] = None
    deployer_balance_raw: Optional[int] = None   # None = UNKNOWN
    deployer_sent_raw: Optional[int] = None
    deployer_received_raw: Optional[int] = None
    deployer_transfers_read: Optional[int] = None
    deployer_transfers_more: Optional[bool] = None
    forensics: Optional[LaunchForensics] = None
    not_read: List[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return bool(self.not_read) or bool(self.forensics and self.forensics.not_read)

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["partial"] = self.partial
        return out


def _err(exc: Exception) -> str:
    return type(exc).__name__


# -- EVM ---------------------------------------------------------------------

def evm_origin(token: str, chain: str, *, fetch: Optional[Callable] = None,
               balance_fn: Optional[Callable] = None) -> OriginReport:
    rep = OriginReport(chain=chain, token=token, available=False,
                       source="Blockscout v2 (keyless)")
    if not _act.blockscout_api(chain):
        rep.reason = (f"origin: NOT AVAILABLE on {chain} — no keyless explorer API is "
                      f"pinned for this chain")
        return rep
    get = lambda path, params=None: _act.blockscout_get(chain, path, params, fetch=fetch)  # noqa: E731
    try:
        info = get(f"addresses/{token}") or {}
    except Exception as exc:
        rep.reason = f"the explorer did not answer for the token ({_err(exc)})"
        return rep
    rep.available = True
    tok = info.get("token") or {}
    rep.symbol, rep.name = tok.get("symbol"), tok.get("name")
    rep.decimals = _act._int(tok.get("decimals"), None)
    if info.get("is_scam") or info.get("reputation") == "scam" or tok.get("reputation") == "scam":
        rep.explorer_flags.append("the explorer marks this address as scam")
    if info.get("is_verified") is False:
        rep.explorer_flags.append("source code not verified on the explorer")
    creator = info.get("creator_address_hash")
    rep.creation_ref = info.get("creation_transaction_hash")
    if not creator and not rep.creation_ref:
        rep.not_read.append("creator: the explorer gives no creator for this address")
        return rep
    deployer = creator
    if rep.creation_ref:
        try:
            ctx = get(f"transactions/{rep.creation_ref}") or {}
            sender = _act._hash(ctx.get("from"))
            rep.creation_time = _act._iso_to_unix(ctx.get("timestamp"))
            if sender and creator and not _act._same(sender, creator):
                # CREATE'd by a contract (a factory or a launchpad); the person-side
                # address is whoever SENT the creation transaction.
                rep.creator_contract = creator
                deployer = sender
            elif sender:
                deployer = sender
        except Exception as exc:
            rep.not_read.append(f"creation transaction ({_err(exc)})")
    rep.deployer = deployer
    if not deployer:
        return rep
    # Its other creations: the deployer's EARLIEST transactions, one page.
    try:
        body = get(f"addresses/{deployer}/transactions",
                   {"sort": "block_number", "order": "asc"}) or {}
        items = body.get("items") or []
        rep.other_creations_read = len(items)
        rep.other_creations_more = bool(body.get("next_page_params"))
        for it in items:
            made = it.get("created_contract") or {}
            addr = made.get("hash")
            if addr and not _act._same(addr, token):
                rep.other_creations.append(Creation(addr, made.get("name"), it.get("hash"),
                                                    _act._iso_to_unix(it.get("timestamp"))))
    except Exception as exc:
        rep.not_read.append(f"deployer's other creations ({_err(exc)})")
    # Does the deployer still hold it, and has it sent it on?
    try:
        from core.wallet import onchain
        got = (balance_fn or onchain.token_balances)(deployer, chain, [token])
        vals = list((got or {}).values())
        rep.deployer_balance_raw = vals[0] if vals else None
    except Exception:
        rep.deployer_balance_raw = None
    if rep.deployer_balance_raw is None:
        rep.not_read.append("deployer's current balance")
    try:
        body = get(f"addresses/{deployer}/token-transfers", {"token": token}) or {}
        items = body.get("items") or []
        sent = recv = 0
        for it in items:
            val = _act._int((it.get("total") or {}).get("value"))
            if _act._same(_act._hash(it.get("from")), deployer):
                sent += val
            if _act._same(_act._hash(it.get("to")), deployer):
                recv += val
        rep.deployer_sent_raw, rep.deployer_received_raw = sent, recv
        rep.deployer_transfers_read = len(items)
        rep.deployer_transfers_more = bool(body.get("next_page_params"))
    except Exception as exc:
        rep.not_read.append(f"deployer's transfers of this token ({_err(exc)})")
    return rep


# -- Solana ------------------------------------------------------------------

def _programs(tx: Dict[str, Any]) -> set:
    out = set()
    msg = (tx.get("transaction") or {}).get("message") or {}
    for ins in msg.get("instructions") or []:
        if ins.get("programId"):
            out.add(ins["programId"])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ins in inner.get("instructions") or []:
            if ins.get("programId"):
                out.add(ins["programId"])
    return out


def _call(call: Callable, method: str, params: list):
    return _act.call_with_backoff(call, method, params)


def _launch_buyers(tx: Dict[str, Any], mint: str, sig: str, slot) -> List[Buyer]:
    """Signers whose balance of *mint* went UP in this transaction. Requiring a
    signer keeps out the bonding curve / pool accounts (program-owned) that
    receive tokens without signing."""
    signers = set(_act._signers(tx))
    out = []
    for (owner, m), (raw, _dec) in _act.token_deltas(tx).items():
        if m == mint and raw > 0 and owner in signers:
            out.append(Buyer(address=owner, raw=raw, ref=sig, slot=slot))
    return out


def _trace_funder(buyer: Buyer, call: Callable, deadline: float) -> None:
    """One hop back: the newest transaction BEFORE the buy in which the buyer's
    SOL went up; the account whose SOL went down most is the funder."""
    if time.monotonic() >= deadline:
        return
    sigs = _call(call, "getSignaturesForAddress",
                 [buyer.address, {"before": buyer.ref, "limit": FUNDER_TX}]) or []
    for entry in sigs:
        if time.monotonic() >= deadline:
            return
        if entry.get("err") is not None or not entry.get("signature"):
            continue
        tx = _act.get_solana_tx(entry["signature"], rpc=call)
        if not tx:
            continue
        sols = _act.sol_deltas(tx)
        if sols.get(buyer.address, 0) <= 0:
            continue
        others = [(v, k) for k, v in sols.items() if k != buyer.address and v < 0]
        if others:
            buyer.funder = min(others)[1]
            buyer.funder_ref = entry["signature"]
            break
    buyer.funder_read = True


def solana_origin(mint: str, *, rpc: Optional[Callable] = None,
                  budget_sec: float = BUDGET_SEC, forensics: bool = True,
                  chain: str = "solana") -> OriginReport:
    call = rpc or _act._rpc
    started = time.monotonic()
    deadline = started + budget_sec
    rep = OriginReport(chain=chain, token=mint, available=False,
                       source="solana RPC (getSignaturesForAddress + getTransaction)")
    before = None
    oldest_page: List[dict] = []
    #: 071 review: the page BEFORE the oldest one too — a short last page (5
    #: entries) would otherwise cut launch-slot transactions out of the window.
    prev_page: List[dict] = []
    reached = False
    for page in range(MAX_SIG_PAGES):
        if time.monotonic() >= deadline:
            break
        opts: Dict[str, Any] = {"limit": SIG_PAGE}
        if before:
            opts["before"] = before
        try:
            sigs = _call(call, "getSignaturesForAddress", [mint, opts]) or []
        except Exception as exc:
            if page == 0:
                rep.reason = f"getSignaturesForAddress failed ({_err(exc)})"
                return rep
            rep.not_read.append(f"signature page {page + 1} ({_err(exc)})")
            break
        rep.available = True
        if sigs:
            prev_page, oldest_page = oldest_page, sigs
            before = sigs[-1].get("signature")
        if len(sigs) < SIG_PAGE:
            reached = True
            break
    if not reached:
        rep.not_read.append(
            f"creation: NOT FOUND within the newest {MAX_SIG_PAGES * SIG_PAGE:,} signatures "
            f"(or the {budget_sec:.0f}s budget) — the mint is older or busier than this "
            f"bounded read")
        rep.forensics = None
        return rep
    if not oldest_page:
        rep.not_read.append("creation: the mint has no signatures")
        return rep
    asc = list(reversed(oldest_page)) + list(reversed(prev_page))
    first = asc[0]
    rep.creation_ref = first.get("signature")
    rep.creation_slot = first.get("slot")
    rep.creation_time = first.get("blockTime")
    try:
        ctx = _act.get_solana_tx(rep.creation_ref, rpc=call) or {}
    except Exception as exc:
        rep.not_read.append(f"creation transaction ({_err(exc)})")
        return rep
    keys = _act._keys(ctx)
    rep.deployer = keys[0] if keys else None
    if PUMP_FUN_PROGRAM in _programs(ctx):
        rep.launchpad = "pump.fun"
    if rep.deployer:
        try:
            res = _call(call, "getTokenAccountsByOwner",
                        [rep.deployer, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
            total, dec = 0, None
            for acc in res.get("value") or []:
                info = ((((acc.get("account") or {}).get("data") or {}).get("parsed") or {})
                        .get("info") or {})
                amt = info.get("tokenAmount") or {}
                total += _act._int(amt.get("amount"))
                dec = amt.get("decimals", dec)
            rep.deployer_balance_raw = total
            if dec is not None:
                rep.decimals = dec
        except Exception as exc:
            rep.not_read.append(f"deployer's current balance ({_err(exc)})")
    if forensics:
        rep.forensics = _forensics(mint, rep, asc, ctx, call, deadline)
    return rep


def _forensics(mint, rep: OriginReport, asc: List[dict], creation_tx, call,
               deadline: float) -> LaunchForensics:
    fx = LaunchForensics(first_slot=rep.creation_slot)
    window = [e for e in asc if e.get("err") is None and e.get("slot") is not None
              and rep.creation_slot is not None
              and e["slot"] <= rep.creation_slot + LAUNCH_SLOTS]
    if len(window) > MAX_LAUNCH_TX:
        fx.not_read.append(f"{len(window) - MAX_LAUNCH_TX} launch transactions past the "
                           f"first {MAX_LAUNCH_TX}")
        window = window[:MAX_LAUNCH_TX]
    buyers: Dict[str, Buyer] = {}
    failed = 0
    for entry in window:
        if time.monotonic() >= deadline:
            fx.not_read.append("launch transactions: the read budget ran out")
            break
        sig = entry["signature"]
        try:
            tx = creation_tx if sig == rep.creation_ref else _act.get_solana_tx(sig, rpc=call)
        except Exception:
            failed += 1
            continue
        if not tx:
            failed += 1
            continue
        fx.launch_tx_read += 1
        if rep.decimals is None:
            for (_who, m), (_raw, dec) in _act.token_deltas(tx).items():
                if m == mint and dec is not None:
                    rep.decimals = dec
                    break
        for b in _launch_buyers(tx, mint, sig, entry.get("slot")):
            if b.address in buyers:
                buyers[b.address].raw += b.raw
            else:
                buyers[b.address] = b
    if failed:
        fx.not_read.append(f"{failed} launch transactions (getTransaction failed)")
    fx.buyers = list(buyers.values())
    traced = fx.buyers[:MAX_BUYERS]
    if len(fx.buyers) > MAX_BUYERS:
        fx.not_read.append(f"funders of {len(fx.buyers) - MAX_BUYERS} buyers past the "
                           f"first {MAX_BUYERS}")
    pool = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        futs = {pool.submit(_trace_funder, b, call, deadline): b for b in traced}
        done, pending = _cf.wait(futs, timeout=max(0.0, deadline - time.monotonic()))
        errs = 0
        for fut in done:
            try:
                fut.result()
            except Exception:
                errs += 1
        untraced = len(pending) + errs
        if untraced:
            fx.not_read.append(f"funders of {untraced} buyers (RPC failed or budget ran out)")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    by_funder: Dict[str, List[str]] = {}
    for b in traced:
        if b.funder:
            by_funder.setdefault(b.funder, []).append(b.address)
    fx.shared_funders = {f: bs for f, bs in by_funder.items() if len(bs) >= 2}
    if rep.deployer:
        fx.deployer_funded = [b.address for b in traced
                              if b.funder == rep.deployer and b.address != rep.deployer]
    return fx


def origin(token: str, chain: str, *, rpc: Optional[Callable] = None,
           fetch: Optional[Callable] = None,
           balance_fn: Optional[Callable] = None) -> OriginReport:
    """Family dispatch. Never raises."""
    from core.wallet import chains
    row = chains.get(chain)
    try:
        if row is not None and row.family == "svm":
            return solana_origin(token, rpc=rpc, chain=chain)
        return evm_origin(token, chain, fetch=fetch, balance_fn=balance_fn)
    except Exception as exc:
        logger.debug("origin read failed for %s on %s", token, chain, exc_info=True)
        return OriginReport(chain=chain, token=token, available=False,
                            reason=f"origin read failed ({_err(exc)})")
