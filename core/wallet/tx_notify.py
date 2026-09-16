"""The ONE money-execution notice rail (039 Unit A).

Until this module, a live transaction told the owner nothing. The only automatic
message any `defi_trade` verb produced was the tiered spend-lane hook reusing the
*receive* lane's text:

    Auto-approved payment request (defi_trade_bridge) (within caps;
    PAYMENT_APPROVAL_MODE=auto).

On 2026-09-12 the owner read that line about a transaction he had approved by hand
twice. It said "auto-approved" (he had not), named a mode that decided nothing, and
carried no chain, no amount, no hash and no arrival. Everything he actually learned
about his own money came from the agent choosing to narrate it in prose.

Two notices per transaction, and only two:

    BROADCAST — what left, where it is going, what it cost, which lane allowed it,
    and the hash to follow. Sent the moment the transaction is accepted, because
    the window between broadcast and settlement is exactly when the owner is blind.

    SETTLED   — what actually happened, measured. Confirmed, reverted, arrived, or
    still in flight. An in-flight bridge gets one too; silence would be the worst
    available answer about money between two chains.

Three rules:

1. **Critical lane, never capped.** `source="tx_execution"` is in
   `user_delivery._CRITICAL_SOURCES`. Over 8 days in August, 195 of 196 owner
   notices were dropped by the shared daily cap, six of them approval asks. A
   notice about money that ALREADY MOVED may not queue behind "▶ goal started".
2. **Not pause-gated.** The 031 pause stops the agent ACTING. This is a report.
   Suppressing it would leave the owner blind to funds in flight at the exact
   moment they asked everything to stop.
3. **A dry run never notifies.** Pinging the owner for each simulation trains them
   to ignore the channel, and a channel nobody reads is worse than no channel.

Unknown is rendered `unknown`, never `$0.00` and never `0` — the confident-zero
class that produced 114 tenantless `wallet_spend` rows and a ledger reading a
clean zero over real money.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The delivery source. Registered in `user_delivery._CRITICAL_SOURCES`, so the
#: daily cap can never drop one of these.
SOURCE = "tx_execution"

#: Terminal and non-terminal settlement states, shared with `bridge_guard`'s
#: vocabulary so one transaction is never described two ways.
STATE_CONFIRMED = "confirmed"
STATE_ARRIVED = "arrived"
STATE_REVERTED = "reverted"
STATE_FAILED = "failed"
STATE_IN_FLIGHT = "in_flight"

_ICON = {
    STATE_CONFIRMED: "✅",
    STATE_ARRIVED: "✅",
    STATE_REVERTED: "❌",
    STATE_FAILED: "❌",
    STATE_IN_FLIGHT: "⏳",
}

_HEADLINE = {
    STATE_CONFIRMED: "CONFIRMED",
    STATE_ARRIVED: "ARRIVED",
    STATE_REVERTED: "REVERTED — the fee was spent, the value did not move",
    STATE_FAILED: "FAILED",
    STATE_IN_FLIGHT: "IN FLIGHT — not a failure. Do NOT re-send",
}


def enabled() -> bool:
    from core.env import bool_env
    return bool_env("TX_NOTIFY_ENABLED", True)


def _usd(value: Optional[float]) -> str:
    return "unknown" if value is None else f"${float(value):,.2f}"


def short(ref: Optional[str], head: int = 6, tail: int = 4) -> str:
    """A hash the owner can match against an explorer without wrapping a line.

    Never truncated to nothing: a ref short enough to fit is shown whole, because
    an elided identifier that cannot be matched is worse than a long one.
    """
    s = str(ref or "").strip()
    if not s:
        return "unknown"
    if len(s) <= head + tail + 1:
        return s
    return f"{s[:head]}…{s[-tail:]}"


@dataclass(frozen=True)
class TxNotice:
    """One transaction, described once.

    Every optional field is optional because it is genuinely unknowable on some
    path — not so a caller can skip it. An omitted field renders as an absent
    line; a field that is present but unreadable renders `unknown`.
    """
    verb: str                                  # bridge | swap | solana_swap | transfer | …
    route: str                                 # "solana→base" or "base"
    amount_in: Optional[str] = None            # "0.93 SOL"
    amount_out: Optional[str] = None           # "≥0.03640 ETH"
    usd: Optional[float] = None
    tx_ref: Optional[str] = None
    #: The chain registry key (`core.wallet.chains`) that `tx_ref` was
    #: BROADCAST on, for the explorer link. ⚠️ For a bridge that is the ORIGIN,
    #: never the destination: `bridge_guard.settle` records the SEND, so a
    #: destination link would put a Solana base58 signature inside an EVM
    #: explorer URL — authoritative-looking and resolving to nothing. An
    #: arrival has no hash of its own; it is proven by `measured`.
    #: Every call site passes this (pinned by
    #: `tests/unit/core/wallet/test_tx_notice_chain_wired.py`). `None` still
    #: renders no link, which is the right answer for a chain that cannot be
    #: named — a wrong link is worse than an absent one.
    chain: Optional[str] = None
    #: A second identifier the owner can chase: the Relay request id, the bridge
    #: row id. Rendered beside the hash, never instead of it.
    extra_refs: tuple = ()
    lane: Optional[str] = None                 # "autonomous" | "owner-approved"
    cap_used_usd: Optional[float] = None
    cap_limit_usd: Optional[float] = None
    #: SETTLED only.
    state: Optional[str] = None
    measured: Optional[str] = None             # "+0.03716593 ETH on base"
    detail: Optional[str] = None
    ledger_recorded: Optional[bool] = None


def _tx_link(n: TxNotice) -> Optional[str]:
    """The explorer link for *n*'s tx hash, or `None` — never raises.

    Deliberately independent of every call site: `chain` and `tx_ref` are both
    optional on `TxNotice`, and most existing callers set neither, so this is
    a no-op (`None`) for them, byte-identical to before the field existed.
    """
    if not n.chain or not n.tx_ref:
        return None
    try:
        from core.wallet.chains import explorer_url
        return explorer_url(n.chain, "tx", str(n.tx_ref))
    except Exception:
        logger.debug("tx_notify: explorer link lookup failed (fail-open)", exc_info=True)
        return None


def render_broadcast(n: TxNotice) -> str:
    lines = [f"⛓ SENT · {n.verb} {n.route}"]
    if n.amount_in:
        # The value is ALWAYS stated, including when it is unknown. An omitted
        # USD line reads as "no value moved" to a skimming eye, which is the same
        # silent-zero this rail exists to end — so say `unknown` out loud.
        head = f"{n.amount_in} ({_usd(n.usd)})"
        if n.amount_out:
            head += f" → {n.amount_out}"
        lines.append(head)
    else:
        lines.append(_usd(n.usd))
    refs = [f"tx {short(n.tx_ref)}"] + [f"ref {short(r)}" for r in n.extra_refs if r]
    lines.append(" · ".join(refs))
    link = _tx_link(n)
    if link:
        lines.append(link)
    tail = []
    if n.cap_limit_usd is not None and n.cap_used_usd is not None:
        tail.append(f"cap: {_usd(n.cap_used_usd)} of {_usd(n.cap_limit_usd)} daily")
    if n.lane:
        tail.append(f"lane {n.lane}")
    if tail:
        lines.append(" · ".join(tail))
    return "\n".join(lines)


def render_settled(n: TxNotice) -> str:
    state = n.state or STATE_IN_FLIGHT
    icon = _ICON.get(state, "⏳")
    head = _HEADLINE.get(state, state.upper())
    lines = [f"{icon} {head} · {n.verb} {n.route}"]
    if n.measured:
        lines.append(f"measured {n.measured}")
    elif n.amount_out:
        # No measurement is NOT the same as the quoted number, and must not be
        # printed as if it were one.
        lines.append(f"quoted {n.amount_out} · not independently measured")
    refs = [f"tx {short(n.tx_ref)}"] + [f"ref {short(r)}" for r in n.extra_refs if r]
    lines.append(" · ".join(refs))
    link = _tx_link(n)
    if link:
        lines.append(link)
    if n.detail:
        lines.append(str(n.detail)[:400])
    if n.ledger_recorded is False:
        lines.append("⚠ ledger: NOT recorded — this spend is invisible to every "
                     "other money verb's cap")
    return "\n".join(lines)


def caps_from_gate(gate: Any, venue: str = "defi") -> tuple:
    """``(used_usd, limit_usd)`` for the rolling 24h window, or ``(None, None)``.

    Fail-open to unknown: a cap line that cannot be read is omitted, never
    rendered as a comfortable zero.
    """
    try:
        limit = gate.daily_cap_usd
        if limit is None:
            return None, None
        return float(gate.rolling_24h_spend_usd()), float(limit)
    except Exception:
        logger.debug("tx_notify: cap read failed (fail-open)", exc_info=True)
        return None, None


async def notify(container: Any, user_id: Optional[str], notice: TxNotice, *,
                 settled: bool, session_id: Optional[str] = None) -> str:
    """Deliver one notice. Never raises, never blocks a settled transaction.

    Returns the delivery outcome, or ``"skipped"``/``"error"``. The return value
    exists for tests and telemetry; no caller branches on it, because a failed
    notification must not change what happened to the money.
    """
    if not enabled():
        return "skipped"
    text = render_settled(notice) if settled else render_broadcast(notice)
    _emit_event(user_id, notice, settled=settled, session_id=session_id)
    if not user_id:
        # A tenantless notice has nowhere to go, and inventing an owner would
        # cross tenants. The event above still records it.
        logger.warning("tx_notify: no user_id for %s %s — event recorded, not delivered",
                       notice.verb, notice.tx_ref)
        return "error"
    try:
        from core.surfaces.user_delivery import deliver_user_message
        return await deliver_user_message(container, str(user_id), text,
                                          source=SOURCE, session_id=session_id,
                                          priority="critical")
    except Exception:
        logger.warning("tx_notify: delivery failed for %s (fail-open)",
                       notice.tx_ref, exc_info=True)
        return "error"


#: Live fire-and-forget tasks. A task with no strong reference can be garbage
#: collected mid-flight, which would drop the notice silently — the exact failure
#: this module exists to end.
_PENDING: set = set()


def notify_soon(container: Any, user_id: Optional[str], notice: TxNotice, *,
                settled: bool, session_id: Optional[str] = None) -> None:
    """Fire a notice from a SYNC call site. Never raises, never blocks.

    The EVM money verbs settle inside a synchronous helper (`_run_guarded`) that
    runs under a live event loop. Scheduling there is right: the owner should not
    wait on a Telegram round trip to learn his transaction confirmed, and a
    notification must never sit between a broadcast and its ledger record.
    """
    coro = None
    try:
        import asyncio
        coro = notify(container, user_id, notice, settled=settled,
                      session_id=session_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(coro)
            _PENDING.add(task)
            task.add_done_callback(_PENDING.discard)
            return
        from core.async_bridge import run_coroutine_sync
        run_coroutine_sync(coro)
    except Exception:
        logger.debug("tx_notify: scheduling failed (fail-open)", exc_info=True)
        if coro is not None:
            try:
                coro.close()
            except Exception:
                pass


def _emit_event(user_id: Optional[str], notice: TxNotice, *, settled: bool,
                session_id: Optional[str]) -> None:
    """The durable audit half. Independent of delivery on purpose: a notice the
    owner never received must still be reconstructable."""
    try:
        from core.event_kinds import TX_BROADCAST, TX_SETTLED
        from core.event_log import event_log_enabled, get_event_log
        if not event_log_enabled():
            return
        get_event_log().record(
            TX_SETTLED if settled else TX_BROADCAST,
            user_id=str(user_id or ""), session_id=str(session_id or ""),
            source=SOURCE,
            attrs={"verb": notice.verb, "route": notice.route,
                   "tx_ref": notice.tx_ref, "usd": notice.usd,
                   "state": notice.state, "lane": notice.lane,
                   "measured": notice.measured})
    except Exception:
        logger.debug("tx_notify: audit event skipped (fail-open)", exc_info=True)
