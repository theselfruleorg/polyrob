"""Delta engine — what a transaction WOULD do, measured rather than assumed.

`tx_guard` bounds a transaction by asserting its observed effects against a
declared intent. For that to mean anything, the effects must be OBSERVED: this
module reads balances and allowances, runs the transaction under `eth_call` with
a state override, reads them again, and reports the differences. It never parses
the caller's calldata to decide what the transaction does — doing so would check
the declaration against itself.

Honest limits, stated rather than papered over:

* **Simulation is not execution.** A contract can behave differently at
  execution time (block-dependent logic, another transaction landing in the same
  block, a contract that detects `eth_call`). Mitigated by simulating at pending
  state and re-simulating immediately pre-broadcast — not eliminated.
* **Simulation cannot see the future.** An allowance granted now can be drained
  in a later transaction this engine will never observe. That is why an
  allowance INCREASE is refused unless explicitly declared, rather than merely
  reported.
* **The RPC is the oracle.** Every number here comes from one endpoint, so a
  lying endpoint yields a lying simulation. `tx_guard` refuses to arm on the
  default public RPC for that reason.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from core.wallet import onchain

logger = logging.getLogger(__name__)

_SEL_BALANCE_OF = "0x70a08231"
_SEL_ALLOWANCE = "0xdd62ed3e"


@dataclass(frozen=True)
class Deltas:
    """Observed effect of a simulated transaction.

    ``ok=False`` means the simulation could not be trusted (revert, RPC error,
    or an unreadable balance) — the guard must refuse, never proceed on partial
    information.
    """
    ok: bool
    native_delta: int = 0
    token_deltas: Dict[str, int] = field(default_factory=dict)
    allowance_deltas: Dict[Tuple[str, str], int] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def grants_allowance(self) -> bool:
        return any(v > 0 for v in self.allowance_deltas.values())


def _default_rpc(method: str, params: list, timeout: float = 8.0):
    return onchain._rpc(onchain.rpc_url_for_chain("base"), method, params, timeout)


def _pad_addr(addr: str) -> str:
    return addr[2:].lower().rjust(64, "0")


def _read_uint(rpc: Callable, chain: str, to: str, data: str) -> Optional[int]:
    raw = rpc("eth_call", [{"to": to, "data": data}, "latest"])
    return onchain._hex_int(raw)


def _balance_of(rpc, chain, token, holder):
    return _read_uint(rpc, chain, token, _SEL_BALANCE_OF + _pad_addr(holder))


def _allowance(rpc, chain, token, owner, spender):
    return _read_uint(rpc, chain, token,
                      _SEL_ALLOWANCE + _pad_addr(owner) + _pad_addr(spender))


def _native_balance(rpc, holder) -> Optional[int]:
    return onchain._hex_int(rpc("eth_getBalance", [holder, "latest"]))


def simulate(tx: dict, *, holder: str, chain: str,
             tokens: List[str], spenders: List[str],
             rpc: Optional[Callable] = None) -> Deltas:
    """Simulate *tx* and return the observed deltas for *holder*.

    ⚠️ Uses ``eth_simulateV1``, NOT a sequence of ``eth_call``s. This is the
    whole correctness of the module: ``eth_call`` does not persist state, so
    reading a balance, running the transaction, and reading it again returns the
    SAME number every time. That produced all-zero deltas, which the guard read
    as "costs nothing" and authorized — a live 0.25 USDC transfer was broadcast
    on 2026-08-09 through a cap that should have refused it. A bundle is the
    only way these reads mean anything.

    ``eth_simulateV1`` executes the calls in order with state carried between
    them, so the layout is: [reads…, THE TX, reads…]. If the node does not
    support it, this refuses — it never falls back to the vacuous form.

    ``tokens`` are the ERC-20 contracts to measure; ``spenders`` are the
    addresses whose allowance over each token should be measured. Anything not
    measured cannot be asserted, so the guard must name every address the
    transaction touches.
    """
    rpc = rpc or _default_rpc

    reads: List[dict] = []
    layout: List[tuple] = []
    for t in tokens:
        reads.append({"from": holder, "to": t,
                      "data": _SEL_BALANCE_OF + _pad_addr(holder)})
        layout.append(("token", t))
    for t in tokens:
        for s in spenders:
            reads.append({"from": holder, "to": t,
                          "data": _SEL_ALLOWANCE + _pad_addr(holder) + _pad_addr(s)})
            layout.append(("allow", (t, s)))

    the_tx = {"from": holder, "to": tx.get("to"),
              "data": tx.get("data", "0x"),
              "value": hex(int(tx.get("value", 0) or 0))}
    calls = reads + [the_tx] + reads
    tx_index = len(reads)

    try:
        result = rpc("eth_simulateV1", [
            {"blockStateCalls": [{"calls": calls}],
             "validation": False, "traceTransfers": False}, "latest"])
    except Exception as exc:
        return Deltas(ok=False, error=(
            f"simulation unavailable ({exc}) — refusing rather than proceeding "
            f"on an unsimulated transaction"))

    try:
        entries = (result or [{}])[0].get("calls") or []
    except Exception:
        entries = []
    if len(entries) != len(calls):
        return Deltas(ok=False, error=(
            "simulation returned an unexpected shape — refusing "
            f"(expected {len(calls)} results, got {len(entries)})"))

    tx_entry = entries[tx_index]
    if str(tx_entry.get("status")) not in ("0x1", "1"):
        return Deltas(ok=False, error=(
            f"the transaction REVERTS in simulation "
            f"({tx_entry.get('error') or 'status 0'})"))

    def _value(entry) -> Optional[int]:
        if str(entry.get("status")) not in ("0x1", "1"):
            return None
        return onchain._hex_int(entry.get("returnData"))

    before = [_value(e) for e in entries[:tx_index]]
    after = [_value(e) for e in entries[tx_index + 1:]]
    if any(v is None for v in before + after):
        return Deltas(ok=False, error=(
            "simulation produced an unknown balance/allowance — refusing rather "
            "than treating unknown as zero"))

    token_deltas: Dict[str, int] = {}
    allowance_deltas: Dict[Tuple[str, str], int] = {}
    for i, (kind, key) in enumerate(layout):
        delta = after[i] - before[i]
        if kind == "token":
            token_deltas[key] = delta
        else:
            allowance_deltas[key] = delta

    return Deltas(ok=True, native_delta=0,
                  token_deltas=token_deltas, allowance_deltas=allowance_deltas)
