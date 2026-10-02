"""Recent activity of ANY wallet, as typed rows (proposal 071 §3.7, W4).

Read-only and keyless:

* **Solana** — ``getSignaturesForAddress`` + ``getTransaction`` (jsonParsed)
  over the public RPC. The owner's SOL change comes from ``pre/postBalances``
  at the owner's account index, token changes from ``pre/postTokenBalances``
  filtered by ``owner``.
* **EVM** — the chain row's keyless Blockscout v2 API
  (``ChainRow.blockscout_api``): ``/transactions``, ``/token-transfers`` and
  ``/internal-transactions`` of the address, grouped by transaction hash.

Rules this module keeps:

* **Bounded.** At most :data:`MAX_TX` transactions and a wall-clock budget;
  what was listed but not read is COUNTED in ``not_read``, never dropped.
* **Unknown is not zero.** A failed read is ``available=False`` with a reason,
  never an empty history; unknown decimals stay ``None``.
* **Tool arithmetic only.** :func:`net_flows` does the sums so no caller (and
  no model) adds raw units by hand. It is a net FLOW over the rows read, NOT a
  cost-basis PnL.
* **base58 is case-sensitive.** Solana addresses are compared byte-for-byte;
  only EVM hex is compared case-insensitively.
"""
from __future__ import annotations

import concurrent.futures as _cf
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

NATIVE = "native"

#: Hard cap on transactions one call reads, whatever the caller asks for.
MAX_TX = 25
#: Wall-clock budget for one activity read.
BUDGET_SEC = 20.0
#: Rent-exempt minimum of one SPL token account. A SOL change no larger than
#: this beside a token move is account rent, not a side of a swap.
RENT_DUST_LAMPORTS = 2_039_280
SOL_DECIMALS = 9


@dataclass(frozen=True)
class Delta:
    """One asset's signed change for the owner in one transaction."""
    asset: str                       # NATIVE, or the token contract / mint
    raw: int                         # signed, smallest units
    decimals: Optional[int]          # None = unknown (render raw units)
    symbol: Optional[str] = None     # attacker-written; render with repr()
    nft: bool = False

    @property
    def amount(self) -> Optional[Decimal]:
        if self.decimals is None:
            return None
        return Decimal(self.raw).scaleb(-int(self.decimals))


@dataclass
class ActivityRow:
    ref: str                         # tx hash / signature
    time: Optional[int]              # unix seconds, None = unknown
    kind: str                        # receive | send | swap | other | failed
    deltas: List[Delta] = field(default_factory=list)
    counterparty: Optional[str] = None
    fee_raw: Optional[int] = None    # paid BY the owner; 0 = someone else paid
    flags: List[str] = field(default_factory=list)


@dataclass
class ActivityReport:
    chain: str
    owner: str
    available: bool
    source: str = ""
    rows: List[ActivityRow] = field(default_factory=list)
    listed: int = 0                  # transactions the list call returned
    not_read: List[str] = field(default_factory=list)
    native_symbol: str = ""
    native_decimals: int = 18
    reason: Optional[str] = None
    more_exist: Optional[bool] = None

    @property
    def partial(self) -> bool:
        return bool(self.not_read)

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["partial"] = self.partial
        return out


# -- shared arithmetic -------------------------------------------------------

def classify_kind(deltas: List[Delta], *, native_dust: int = 0) -> str:
    """receive / send / swap (two-sided) / other, from the signs alone.

    A native change no larger than ``native_dust`` beside a token move is
    rent or a tip, not a side of the trade.
    """
    tokens = [d for d in deltas if d.asset != NATIVE and d.raw]
    signs = {d.raw > 0 for d in tokens}
    for d in deltas:
        if d.asset == NATIVE and d.raw and (not tokens or abs(d.raw) > native_dust):
            signs.add(d.raw > 0)
    if signs == {True, False}:
        return "swap"
    if signs == {True}:
        return "receive"
    if signs == {False}:
        return "send"
    return "other"


def net_flows(rows: List[ActivityRow]) -> List[Dict[str, Any]]:
    """Per-asset IN / OUT / NET over *rows*, in raw units and (when the
    decimals are known) human units. Fees are not in the deltas; they are
    summed separately by :func:`fees_paid`. NOT a cost-basis PnL."""
    acc: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        for d in row.deltas:
            slot = acc.setdefault(d.asset, {"asset": d.asset, "symbol": d.symbol,
                                            "decimals": d.decimals, "nft": d.nft,
                                            "in_raw": 0, "out_raw": 0, "rows": 0})
            if slot["decimals"] is None and d.decimals is not None:
                slot["decimals"] = d.decimals
            if slot["symbol"] is None and d.symbol:
                slot["symbol"] = d.symbol
            if d.raw > 0:
                slot["in_raw"] += d.raw
            else:
                slot["out_raw"] += -d.raw
            slot["rows"] += 1
    out = []
    for slot in acc.values():
        slot["net_raw"] = slot["in_raw"] - slot["out_raw"]
        dec = slot["decimals"]
        for k in ("in", "out", "net"):
            slot[k] = (None if dec is None
                       else str(Decimal(slot[f"{k}_raw"]).scaleb(-int(dec)).normalize()))
        out.append(slot)
    out.sort(key=lambda s: (s["asset"] != NATIVE, -s["rows"]))
    return out


def fees_paid(rows: List[ActivityRow]) -> Optional[int]:
    """Sum of fees the owner paid, raw native units; ``None`` if any is unknown."""
    total = 0
    for row in rows:
        if row.fee_raw is None:
            return None
        total += row.fee_raw
    return total


# -- Solana ------------------------------------------------------------------

def _rpc(method: str, params: list, timeout: float = 8.0):
    """The ONE network entry for the Solana reads here (blocked in unit tests)."""
    from core.wallet import solana_onchain
    return solana_onchain._rpc(method, params, timeout=timeout)


def _keys(tx: Dict[str, Any]) -> List[str]:
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return [k.get("pubkey") if isinstance(k, dict) else k for k in keys]


def _signers(tx: Dict[str, Any]) -> List[str]:
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return [k.get("pubkey") for k in keys if isinstance(k, dict) and k.get("signer")]


def sol_deltas(tx: Dict[str, Any]) -> Dict[str, int]:
    """{account -> lamport change, fee ADDED BACK for the fee payer}."""
    meta = tx.get("meta") or {}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    keys = _keys(tx)
    out: Dict[str, int] = {}
    for i, key in enumerate(keys):
        if i < len(pre) and i < len(post) and key:
            out[key] = out.get(key, 0) + int(post[i]) - int(pre[i])
    if keys and keys[0] in out:
        out[keys[0]] += int(meta.get("fee") or 0)
    return out


def token_deltas(tx: Dict[str, Any]) -> Dict[tuple, tuple]:
    """{(owner, mint) -> (raw change, decimals)} over every token account."""
    meta = tx.get("meta") or {}
    acc: Dict[tuple, list] = {}
    for sign, key in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for b in meta.get(key) or []:
            owner, mint = b.get("owner"), b.get("mint")
            ui = b.get("uiTokenAmount") or {}
            if not owner or not mint:
                continue
            try:
                raw = int(ui.get("amount") or 0)
            except (TypeError, ValueError):
                continue
            slot = acc.setdefault((owner, mint), [0, ui.get("decimals")])
            slot[0] += sign * raw
            if slot[1] is None:
                slot[1] = ui.get("decimals")
    return {k: (v[0], v[1]) for k, v in acc.items()}


def parse_solana_tx(tx: Dict[str, Any], owner: str, *, sig: str,
                    block_time: Optional[int] = None) -> ActivityRow:
    meta = tx.get("meta") or {}
    keys = _keys(tx)
    payer = keys[0] if keys else None
    fee = int(meta.get("fee") or 0)
    when = tx.get("blockTime") or block_time
    if meta.get("err") is not None:
        return ActivityRow(ref=sig, time=when, kind="failed",
                           fee_raw=fee if payer == owner else 0)
    sols = sol_deltas(tx)
    toks = token_deltas(tx)
    deltas: List[Delta] = []
    own_sol = sols.get(owner, 0)
    if own_sol:
        deltas.append(Delta(NATIVE, own_sol, SOL_DECIMALS, "SOL"))
    for (who, mint), (raw, dec) in toks.items():
        if who == owner and raw:
            deltas.append(Delta(mint, raw, dec))
    kind = classify_kind(deltas, native_dust=RENT_DUST_LAMPORTS)
    counterparty = None
    moved = [d for d in deltas if d.asset != NATIVE] or deltas
    if kind in ("receive", "send") and len(moved) == 1:
        d = moved[0]
        if d.asset == NATIVE:
            cands = [k for k, v in sols.items() if k != owner and v == -d.raw]
        else:
            cands = [w for (w, m), (r, _) in toks.items()
                     if m == d.asset and w != owner and r == -d.raw]
        if len(cands) == 1:
            counterparty = cands[0]
    return ActivityRow(ref=sig, time=when, kind=kind, deltas=deltas,
                       counterparty=counterparty,
                       fee_raw=fee if payer == owner else 0)


#: Version-1 transactions exist on mainnet (measured 2026-10-02: half of a busy
#: address's recent signatures answered "Transaction version (1) is not
#: supported" under ``maxSupportedTransactionVersion: 0``).
MAX_TX_VERSION = 1


def _is_rate_limit(exc: Exception) -> bool:
    return getattr(exc, "code", None) == 429 or "429" in str(exc)


def call_with_backoff(call: Callable, method: str, params: list, *, tries: int = 4,
                      sleep: Callable[[float], None] = time.sleep):
    """One RPC call, retried on HTTP 429 only (the public endpoint throttles
    bursts). Every other failure raises at once."""
    for attempt in range(tries):
        try:
            return call(method, params)
        except Exception as exc:
            if attempt + 1 >= tries or not _is_rate_limit(exc):
                raise
            sleep(0.5 * (2 ** attempt))


def get_solana_tx(sig: str, *, rpc: Optional[Callable] = None, timeout: float = 8.0):
    call = rpc or _rpc
    opts = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": MAX_TX_VERSION}
    try:
        return call_with_backoff(call, "getTransaction", [sig, opts])
    except Exception as exc:
        # An RPC that predates v1 rejects the parameter itself; ask again at 0.
        if "-32602" not in str(exc) and "Invalid param" not in str(exc):
            raise
        return call_with_backoff(call, "getTransaction",
                                 [sig, {**opts, "maxSupportedTransactionVersion": 0}])


def solana_activity(owner: str, limit: int = 20, *, rpc: Optional[Callable] = None,
                    budget_sec: float = BUDGET_SEC, workers: int = 2,
                    chain: str = "solana") -> ActivityReport:
    call = rpc or _rpc
    limit = max(1, min(int(limit or 20), MAX_TX))
    rep = ActivityReport(chain=chain, owner=owner, available=False,
                         source="solana RPC (getSignaturesForAddress + getTransaction)",
                         native_symbol="SOL", native_decimals=SOL_DECIMALS)
    started = time.monotonic()
    try:
        sigs = call("getSignaturesForAddress", [owner, {"limit": limit}]) or []
    except Exception as exc:
        rep.reason = f"getSignaturesForAddress failed ({type(exc).__name__}: {exc})"[:300]
        return rep
    rep.available = True
    rep.listed = len(sigs)
    rep.more_exist = len(sigs) >= limit
    results: Dict[str, Any] = {}
    failed: List[str] = []

    def _one(entry):
        return get_solana_tx(entry["signature"], rpc=call)

    pool = _cf.ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futs = {pool.submit(_one, e): e for e in sigs if e.get("signature")}
        remaining = budget_sec - (time.monotonic() - started)
        done, pending = _cf.wait(futs, timeout=max(0.0, remaining))
        for fut in done:
            entry = futs[fut]
            try:
                tx = fut.result()
            except Exception as exc:
                failed.append(f"{type(exc).__name__}")
                continue
            if not tx:
                failed.append("empty")
                continue
            results[entry["signature"]] = parse_solana_tx(
                tx, owner, sig=entry["signature"], block_time=entry.get("blockTime"))
        if pending:
            rep.not_read.append(f"{len(pending)} of {len(sigs)} transactions: "
                                f"the {budget_sec:.0f}s read budget ran out")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if failed:
        rep.not_read.append(f"{len(failed)} of {len(sigs)} transactions: getTransaction "
                            f"failed ({', '.join(sorted(set(failed)))})")
    rep.rows = [results[e["signature"]] for e in sigs if e.get("signature") in results]
    return rep


# -- EVM (Blockscout) ----------------------------------------------------------

def blockscout_api(chain: str) -> Optional[str]:
    from core.wallet import chains
    row = chains.get(chain)
    return getattr(row, "blockscout_api", None) if row is not None else None


def _get(url: str, timeout: float = 15.0):
    """The ONE network entry for the Blockscout reads (blocked in unit tests)."""
    from core.wallet.rpc_response import read_response
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(url, headers={
        "accept": "application/json", "user-agent": "polyrob-wallet/1.0",
        "accept-encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return read_response(r, deadline=deadline)


def blockscout_get(chain: str, path: str, params: Optional[Dict[str, str]] = None, *,
                   fetch: Optional[Callable] = None):
    base = blockscout_api(chain)
    if not base:
        raise LookupError(f"no keyless explorer API for {chain}")
    url = f"{base}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return (fetch or _get)(url)


def _iso_to_unix(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _hash(side) -> Optional[str]:
    if isinstance(side, dict):
        return side.get("hash")
    return side if isinstance(side, str) else None


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a and b and a.lower() == b.lower())


def _int(raw, default: Optional[int] = 0) -> Optional[int]:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def evm_activity(owner: str, chain: str, limit: int = 20, *,
                 fetch: Optional[Callable] = None) -> ActivityReport:
    from core.wallet import chains
    row = chains.get(chain)
    limit = max(1, min(int(limit or 20), MAX_TX))
    rep = ActivityReport(chain=chain, owner=owner, available=False,
                         source="Blockscout v2 (keyless)",
                         native_symbol=getattr(row, "native_symbol", "") or "native",
                         native_decimals=getattr(row, "native_decimals", 18) or 18)
    if not blockscout_api(chain):
        rep.reason = (f"history: NOT AVAILABLE on {chain} — no keyless explorer API "
                      f"is pinned for this chain")
        return rep
    lists: Dict[str, Optional[dict]] = {}
    paths = ("transactions", "token-transfers", "internal-transactions")
    with _cf.ThreadPoolExecutor(max_workers=3) as pool:
        futs = {p: pool.submit(blockscout_get, chain, f"addresses/{owner}/{p}", fetch=fetch)
                for p in paths}
        for path in paths:
            try:
                lists[path] = futs[path].result() or {}
            except Exception as exc:
                lists[path] = None
                rep.not_read.append(f"{path} ({type(exc).__name__})")
    if lists["transactions"] is None and lists["token-transfers"] is None:
        rep.reason = "the explorer did not answer (" + "; ".join(rep.not_read) + ")"
        rep.not_read = []
        return rep
    rep.available = True
    # Two lists that each stop at one page cover DIFFERENT time windows; a row
    # older than the newest list cut could be missing its other half.
    cut = None
    for path, body in lists.items():
        if body and body.get("next_page_params") and body.get("items"):
            oldest = min((_iso_to_unix(i.get("timestamp")) or 0) for i in body["items"])
            cut = oldest if cut is None else max(cut, oldest)
    rows: Dict[str, ActivityRow] = {}
    native_dec = rep.native_decimals

    def _row(ref, ts):
        r = rows.get(ref)
        if r is None:
            r = rows[ref] = ActivityRow(ref=ref, time=_iso_to_unix(ts), kind="other", fee_raw=0)
        return r

    native: Dict[str, int] = {}
    for tx in (lists["transactions"] or {}).get("items") or []:
        ref = tx.get("hash")
        if not ref:
            continue
        r = _row(ref, tx.get("timestamp"))
        frm, to = _hash(tx.get("from")), _hash(tx.get("to"))
        ok = tx.get("status") in (None, "ok")
        if _same(frm, owner):
            fee = _int((tx.get("fee") or {}).get("value"), None)
            r.fee_raw = fee
        if not ok:
            r.flags.append("failed")
            continue
        value = _int(tx.get("value"))
        if value:
            if _same(frm, owner):
                native[ref] = native.get(ref, 0) - value
                r.counterparty = r.counterparty or to
            if _same(to, owner):
                native[ref] = native.get(ref, 0) + value
                r.counterparty = r.counterparty or frm
    for it in (lists["internal-transactions"] or {}).get("items") or []:
        ref = it.get("transaction_hash")
        if not ref or it.get("success") is False:
            continue
        value = _int(it.get("value"))
        if not value:
            continue
        frm, to = _hash(it.get("from")), _hash(it.get("to"))
        if not (_same(frm, owner) or _same(to, owner)):
            continue
        r = _row(ref, it.get("timestamp"))
        if _same(frm, owner):
            native[ref] = native.get(ref, 0) - value
        if _same(to, owner):
            native[ref] = native.get(ref, 0) + value
    tok: Dict[str, Dict[str, Delta]] = {}
    sides: Dict[str, List[str]] = {}
    for tt in (lists["token-transfers"] or {}).get("items") or []:
        ref = tt.get("transaction_hash")
        token = tt.get("token") or {}
        addr = token.get("address_hash") or token.get("address")
        if not ref or not addr:
            continue
        frm, to = _hash(tt.get("from")), _hash(tt.get("to"))
        sign = (1 if _same(to, owner) else 0) - (1 if _same(frm, owner) else 0)
        if not sign:
            continue
        r = _row(ref, tt.get("timestamp"))
        total = tt.get("total") or {}
        nft = (tt.get("token_type") or token.get("type") or "ERC-20") != "ERC-20"
        raw = _int(total.get("value"), None)
        if raw is None:
            raw = 1 if nft else 0
        dec = _int(total.get("decimals") if total.get("decimals") is not None
                   else token.get("decimals"), None)
        if nft and dec is None:
            dec = 0
        cur = tok.setdefault(ref, {}).get(addr)
        tok[ref][addr] = Delta(addr, (cur.raw if cur else 0) + sign * raw, dec,
                               token.get("symbol"), nft)
        sides.setdefault(ref, []).append(frm if sign > 0 else to)
        if token.get("reputation") == "scam" or (isinstance(tt.get("from"), dict)
                                                  and tt["from"].get("is_scam")):
            if "explorer marks scam" not in r.flags:
                r.flags.append("explorer marks scam")
    for ref, r in rows.items():
        deltas: List[Delta] = []
        if native.get(ref):
            deltas.append(Delta(NATIVE, native[ref], native_dec, rep.native_symbol))
        deltas += [d for d in tok.get(ref, {}).values() if d.raw]
        r.deltas = deltas
        if "failed" in r.flags:
            r.kind = "failed"
            r.flags.remove("failed")
            continue
        r.kind = classify_kind(deltas)
        if r.kind in ("receive", "send") and ref in tok and len(set(sides.get(ref, []))) == 1:
            r.counterparty = sides[ref][0]
        elif r.kind not in ("receive", "send"):
            r.counterparty = None
    ordered = sorted(rows.values(), key=lambda r: -(r.time or 0))
    if cut is not None:
        kept = [r for r in ordered if (r.time or 0) >= cut]
        if len(kept) < len(ordered):
            rep.not_read.append(f"{len(ordered) - len(kept)} older rows: the explorer lists "
                                f"cover different windows, so their other half may be missing")
        ordered = kept
    rep.listed = len(ordered)
    rep.more_exist = cut is not None or len(ordered) > limit
    rep.rows = ordered[:limit]
    return rep


def activity(owner: str, chain: str, limit: int = 20, *, rpc: Optional[Callable] = None,
             fetch: Optional[Callable] = None) -> ActivityReport:
    """Family dispatch. Never raises."""
    from core.wallet import chains
    row = chains.get(chain)
    try:
        if row is not None and row.family == "svm":
            return solana_activity(owner, limit, rpc=rpc, chain=chain)
        return evm_activity(owner, chain, limit, fetch=fetch)
    except Exception as exc:
        logger.debug("activity read failed for %s on %s", owner, chain, exc_info=True)
        return ActivityReport(chain=chain, owner=owner, available=False,
                              reason=f"history read failed ({type(exc).__name__})")
