"""Nothing was watching `bridges.db` (039 Unit C).

`bridge_guard.await_arrival` polls for up to five minutes inside the turn, and if
the funds have not landed it writes the row `in_flight` and returns. That is the
right answer — an unarrived bridge is neither success nor failure, and reporting
failure invites the re-send that pays twice. But it was also the LAST thing that
ever happened to that row. Nothing re-read it, nothing re-measured the
destination, and the owner's only way to find out was to run
`polyrob wallet bridges` and know to ask.

This ticker closes that. Every pass re-measures the destination balance for each
unresolved row, settles the ones that have landed or been refunded, and tells the
owner. It also refreshes the balance cache Unit D renders from, because it is
already making the same kind of read and a second schedule would be a second
thing to keep alive.

⚠️ **Deliberately NOT pause-gated.** The 031 pause stops the agent from ACTING.
This ticker signs nothing, sends nothing on-chain and starts no work; it reads
balances and reports. An owner who has just stopped everything is exactly the
owner who most needs to know where their in-flight funds are, and a pause that
also switched off the answer would make the stop button frightening to press.
The same reasoning already exempts the cold-start requeue and `payment_unmatched`.
`tests/unit/core/wallet/test_bridge_watcher.py` pins this so it is not "fixed".
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: How often the runtime runs a pass. Relay settles in seconds; this is for the
#: rows that did NOT, so it is paced for a human's attention, not a chain's.
INTERVAL_SEC = 120

#: How long an unresolved bridge may sit before the owner is told again. A stuck
#: bridge is worth one alert, not one per tick — an alert that repeats is an alert
#: that gets muted.
ESCALATE_AFTER_SEC = 900


def enabled() -> bool:
    """Rides `DEFI_BRIDGE_ENABLED`, with its own off switch.

    A deployment that cannot bridge has nothing to watch, so the default follows
    the bridge. `BRIDGE_WATCHER_ENABLED=false` turns it off independently.
    """
    from core.env import bool_env
    return bool_env("BRIDGE_WATCHER_ENABLED", bool_env("DEFI_BRIDGE_ENABLED", False))


@dataclass
class TickResult:
    checked: int = 0
    arrived: int = 0
    failed: int = 0
    escalated: int = 0
    unreadable: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {"checked": self.checked, "arrived": self.arrived,
                "failed": self.failed, "escalated": self.escalated,
                "unreadable": self.unreadable}


def _route(row: Dict[str, Any], chain_name: Optional[str]) -> str:
    return f"→{chain_name or row.get('dest_chain_id') or '?'}"


async def tick(container: Any = None, *, db_path: Optional[str] = None,
               provider: Any = None, read_balance: Optional[Callable] = None,
               notify: Optional[Callable] = None, now: Callable[[], float] = time.time
               ) -> TickResult:
    """One reconciliation pass. Never raises."""
    from core.wallet import bridge_guard

    result = TickResult()
    try:
        rows: List[Dict[str, Any]] = bridge_guard.open_bridges_all(db_path=db_path)
    except Exception:
        # An unreadable store is NOT "no bridges in flight". Say nothing rather
        # than imply the board is clear.
        logger.warning("bridge watcher: could not read the bridge store", exc_info=True)
        return result

    if not rows:
        return result

    # Per-row, below: the reader depends on WHAT IS ARRIVING (`currency_out`), so
    # it cannot be one function for the whole pass. An injected reader (tests)
    # still overrides.
    reader = read_balance
    if provider is None:
        # The seam, NOT the provider: `core` may not import `tools`. See
        # core/wallet/bridge_status.py for why the degraded case is safe.
        from core.wallet import bridge_status
        provider = _SeamProvider(bridge_status.read_status)
    notifier = notify or _default_notify

    for row in rows:
        result.checked += 1
        try:
            await _reconcile(row, container=container, provider=provider,
                             reader=reader, notify=notifier, result=result,
                             db_path=db_path, now=now)
        except Exception:
            logger.warning("bridge watcher: row %s failed to reconcile",
                           row.get("id"), exc_info=True)
    return result


async def _reconcile(row: Dict[str, Any], *, container, provider, reader, notify,
                     result: TickResult, db_path, now) -> None:
    from core.wallet import bridge_guard, tx_notify

    bid = row.get("id")
    chain_name = bridge_guard.chain_name_for_id(int(row.get("dest_chain_id") or 0))
    # ⚠️ The explorer link belongs to the ORIGIN, because `tx_ref` on this row IS
    # the origin transaction (`bridge_guard.settle` records the SEND). Linking it
    # to `chain_name`, the DESTINATION, would put a Solana base58 signature
    # inside an EVM explorer URL. A Solana origin is stored under the provider's
    # own pseudo chain id, which this tier cannot resolve (core may not import
    # `tools`) — so it names nothing and the notice renders NO link, which is the
    # right answer: a wrong link is worse than an absent one.
    origin_name = bridge_guard.chain_name_for_id(row.get("origin_chain_id"))
    recipient = row.get("recipient")
    before = row.get("balance_before")
    min_out = int(row.get("min_out_raw") or 0)
    route = _route(row, chain_name)
    user_id = row.get("user_id")
    age = float(now()) - float(row.get("created_at") or 0.0)

    delta = None
    if chain_name and recipient and before is not None:
        measure = reader or bridge_guard.arrival_reader(row.get("currency_out"))
        after = measure(recipient, chain_name)
        if after is None:
            # UNKNOWN, never zero: a dead RPC reporting 0 is indistinguishable
            # from funds that never arrived, and only one of those is an incident.
            result.unreadable += 1
        else:
            delta = int(after) - int(before)
            if min_out > 0 and delta >= min_out:
                detail = (f"arrived on a later pass: measured +{delta} raw on "
                          f"{chain_name} (floor {min_out})")
                bridge_guard.settle(bid, state=bridge_guard.STATE_ARRIVED,
                                    detail=detail, balance_after=int(after),
                                    db_path=db_path)
                result.arrived += 1
                await notify(container, user_id, tx_notify.TxNotice(
                    verb="bridge", route=route, chain=origin_name,
                    tx_ref=row.get("tx_ref"),
                    extra_refs=(bid,), state=tx_notify.STATE_ARRIVED,
                    usd=row.get("amount_usd"),
                    measured=f"+{delta} raw on {chain_name}",
                    detail=f"resolved by the watcher after {int(age)}s",
                    ledger_recorded=True))
                return

    state, status_detail = provider.status(str(row.get("request_id") or ""))
    if state == "failure" and (delta is None or delta < min_out):
        # The one case that can be called failed: the provider says refunded or
        # expired AND no inflow was measured at the destination.
        bridge_guard.settle(
            bid, state=bridge_guard.STATE_FAILED,
            detail=(f"relay reports failure/refund ({status_detail}); measured "
                    f"delta {'unknown' if delta is None else delta}"),
            db_path=db_path)
        result.failed += 1
        await notify(container, user_id, tx_notify.TxNotice(
            verb="bridge", route=route, chain=origin_name,
            tx_ref=row.get("tx_ref"),
            extra_refs=(bid,), state=tx_notify.STATE_FAILED,
            usd=row.get("amount_usd"),
            detail=f"relay reports failure/refund: {status_detail}",
            ledger_recorded=True))
        return

    last = float(row.get("escalated_at") or 0.0)
    if age >= ESCALATE_AFTER_SEC and (now() - last) >= ESCALATE_AFTER_SEC:
        bridge_guard.mark_escalated(bid, at=now(), db_path=db_path)
        result.escalated += 1
        await notify(container, user_id, tx_notify.TxNotice(
            verb="bridge", route=route, chain=origin_name,
            tx_ref=row.get("tx_ref"),
            extra_refs=(bid,), state=tx_notify.STATE_IN_FLIGHT,
            usd=row.get("amount_usd"),
            measured=(None if delta is None else f"{delta} raw on {chain_name}"),
            detail=(f"still unresolved after {int(age // 60)}m. relay says: "
                    f"{status_detail or state}. Do NOT re-send — check the relay "
                    f"request id."),
            ledger_recorded=True))


class _SeamProvider:
    """Adapts the registered status reader to the `.status()` shape the rest of
    this module and `bridge_guard` already speak."""

    def __init__(self, read):
        self._read = read

    def status(self, request_id):
        return self._read(request_id)


async def _default_notify(container, user_id, notice) -> None:
    from core.wallet import tx_notify
    await tx_notify.notify(container, user_id, notice, settled=True)


def refresh_balances(container: Any = None, *, data_home: Optional[str] = None) -> bool:
    """Refresh Unit D's balance cache. Returns whether a snapshot was written.

    Rides this ticker rather than owning a schedule: it is the same kind of read,
    on the same cadence, and a second ticker is a second thing that can silently
    stop.
    """
    try:
        from core.env import bool_env
        if not bool_env("WALLET_CONTEXT_VISIBLE", True):
            return False
        wallet = None
        if container is not None:
            try:
                wallet = container.get_service("agent_wallet")
            except Exception:
                wallet = None
        if wallet is None:
            from core.wallet.factory import get_agent_wallet
            wallet = get_agent_wallet()
        if wallet is None:
            return False
        from core.wallet import balance_cache
        balance_cache.refresh(wallet, data_home)
        return True
    except Exception:
        logger.debug("bridge watcher: balance refresh skipped (fail-open)",
                     exc_info=True)
        return False
