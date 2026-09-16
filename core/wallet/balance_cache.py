"""What the agent is holding, cheap enough to show it every turn (039 Unit D).

The agent could always LOOK at its balances — `defi_data.portfolio` has worked
for a long time. What it could not do was KNOW them without being asked to check,
and an agent that has to be told to look is an agent that answers "how much ETH do
I have?" from whatever happened to be in context. On 2026-08-28 that produced a
published claim that the book was flat while three positions were open.

`live_health.py` deliberately builds its per-turn note with no network reads, and
that is right: a turn must not wait on four JSON-RPC round trips. So the reads
happen on the autonomy runtime's ticker and land here, and the turn reads a file.

Three properties the rest of the system depends on:

* **A read never CREATES the store.** No file means no snapshot has been taken,
  which is a real answer. `_init`-ing on read would leave an empty cache in
  whatever data home happened to resolve — the same class of bug as a status
  surface that reports a confident zero.
* **Unreadable is `None`, and `None` renders `unknown`.** A dead RPC returning
  zero is indistinguishable from an empty wallet, and only one of those is an
  incident.
* **Every row carries its age.** A balance with no timestamp invites the agent to
  treat a six-hour-old number as current. The renderer says how old it is, and a
  stale snapshot says so rather than quietly passing for fresh.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Beyond this the snapshot is rendered as STALE rather than as a balance. It is
#: not a refusal — a six-hour-old number is still evidence — but the agent must
#: never present it as the current one.
STALE_AFTER_SEC = 1800


def cache_path(data_home: Optional[str] = None) -> str:
    """``<data_home>/wallet_balances.json``. Resolved at CALL time, never bound at
    import (`tests/test_home_binding_ratchet.py`)."""
    if data_home:
        return os.path.join(str(data_home), "wallet_balances.json")
    from core.runtime_paths import resolve_data_home
    return os.path.join(str(resolve_data_home()), "wallet_balances.json")


@dataclass
class ChainBalance:
    chain: str
    symbol: str
    #: None is UNKNOWN, never zero. Every consumer branches on it.
    native: Optional[float] = None
    usdc: Optional[float] = None
    #: Why the read failed, when it did. An unexplained blank teaches nothing.
    error: Optional[str] = None


@dataclass
class BalanceSnapshot:
    address: Optional[str] = None
    solana_address: Optional[str] = None
    taken_at: float = 0.0
    chains: List[ChainBalance] = field(default_factory=list)

    @property
    def age_sec(self) -> float:
        return max(0.0, time.time() - float(self.taken_at or 0.0))

    @property
    def stale(self) -> bool:
        return self.age_sec > STALE_AFTER_SEC


def read(data_home: Optional[str] = None) -> Optional[BalanceSnapshot]:
    """The last snapshot, or None when none was ever taken.

    None and "an empty wallet" are different answers and must stay different.
    """
    path = cache_path(data_home)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return BalanceSnapshot(
            address=raw.get("address"), solana_address=raw.get("solana_address"),
            taken_at=float(raw.get("taken_at") or 0.0),
            chains=[ChainBalance(**row) for row in (raw.get("chains") or [])])
    except Exception:
        logger.debug("balance cache: unreadable (fail-open to no snapshot)",
                     exc_info=True)
        return None


def write(snapshot: BalanceSnapshot, data_home: Optional[str] = None) -> None:
    """Atomic replace, so a crashed write never leaves a half-parsed cache that
    reads as a wallet with fewer assets than it has."""
    path = cache_path(data_home)
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"address": snapshot.address,
                       "solana_address": snapshot.solana_address,
                       "taken_at": snapshot.taken_at,
                       "chains": [asdict(c) for c in snapshot.chains]}, fh)
        os.replace(tmp, path)
    except Exception:
        logger.debug("balance cache: write failed (fail-open)", exc_info=True)
        try:
            os.path.exists(tmp) and os.remove(tmp)
        except Exception:
            pass


def collect(wallet: Any, *, evm_reader=None, solana_reader=None,
            now=time.time) -> BalanceSnapshot:
    """Read every chain value can move on, plus Solana. Network-bound.

    Called from the ticker, never from a turn. A per-chain failure is recorded on
    that chain's row and does not abort the others: one dead RPC must not blank
    the whole wallet.
    """
    from core.wallet import chains as _chains

    address = getattr(wallet, "address", None)
    snap = BalanceSnapshot(address=address, taken_at=now())

    if evm_reader is None:
        from core.wallet.onchain import balances as evm_reader  # noqa: N813

    for row in _chains.evm_rows():
        if not row.money_enabled or not address:
            continue
        try:
            native, usdc = evm_reader(address, row.name)
            snap.chains.append(ChainBalance(
                chain=row.name, symbol=row.native_symbol or "ETH",
                native=native, usdc=usdc))
        except Exception as exc:
            snap.chains.append(ChainBalance(
                chain=row.name, symbol=row.native_symbol or "ETH",
                error=f"{type(exc).__name__}: {exc}"[:120]))

    try:
        sol_addr = getattr(wallet, "solana_address", None)
    except Exception:
        sol_addr = None
    if sol_addr:
        snap.solana_address = sol_addr
        if solana_reader is None:
            from core.wallet.solana_onchain import native_balance as solana_reader  # noqa: N813
        try:
            snap.chains.append(ChainBalance(chain="solana", symbol="SOL",
                                            native=solana_reader(sol_addr)))
        except Exception as exc:
            snap.chains.append(ChainBalance(
                chain="solana", symbol="SOL",
                error=f"{type(exc).__name__}: {exc}"[:120]))
    return snap


def refresh(wallet: Any, data_home: Optional[str] = None, **kw) -> BalanceSnapshot:
    snap = collect(wallet, **kw)
    write(snap, data_home)
    return snap


def _amount(value: Optional[float], symbol: str) -> str:
    if value is None:
        return f"unknown {symbol}"
    # 6 decimals shows a gas tank running dry; more is noise on a screen.
    return f"{value:.6f}".rstrip("0").rstrip(".") + f" {symbol}"


def render_lines(snapshot: Optional[BalanceSnapshot]) -> List[str]:
    """One line per chain, ETH first, unknown said out loud.

    ETH leads because it is what the wallet actually runs on: it is gas on every
    money chain here AND, on Robinhood, the quote asset that buys the positions.
    A wallet view that leads with a stablecoin balance describes a treasury this
    agent does not have.
    """
    if snapshot is None:
        return ["wallet: no balance snapshot yet (the reader has not run)"]
    if not snapshot.chains:
        return [f"wallet: no readable chains "
                f"({'no address' if not snapshot.address else 'none armed'})"]

    age = int(snapshot.age_sec)
    stamp = f"{age}s ago" if age < 90 else f"{age // 60}m ago"
    head = f"wallet {snapshot.address or '?'} (read {stamp}"
    head += ", ⚠ STALE)" if snapshot.stale else ")"
    lines = [head]
    for row in snapshot.chains:
        if row.error:
            lines.append(f"  {row.chain}: unknown — {row.error}")
            continue
        part = f"  {row.chain}: {_amount(row.native, row.symbol)}"
        if row.usdc is not None:
            part += f" · {row.usdc:,.2f} USDC"
        lines.append(part)
    return lines


def total_native_by_symbol(snapshot: Optional[BalanceSnapshot]) -> Dict[str, Optional[float]]:
    """Native holdings summed per symbol, for the one-line headline.

    A symbol whose every reading failed is ``None`` — UNKNOWN — not 0.0. Summing
    an unreadable chain as zero is how a wallet reports itself emptier than it is.
    """
    out: Dict[str, Optional[float]] = {}
    seen_unknown: Dict[str, bool] = {}
    for row in (snapshot.chains if snapshot else []):
        sym = row.symbol or "?"
        if row.native is None:
            seen_unknown.setdefault(sym, True)
            out.setdefault(sym, None)
            continue
        out[sym] = (out.get(sym) or 0.0) + float(row.native)
    return out
