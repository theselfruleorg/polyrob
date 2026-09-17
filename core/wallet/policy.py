"""PolicyGate: cheap, backend-independent hygiene for value-moving actions.

Enforces finite per-transaction and rolling caps, replay protection, and a
durable audit trail. Persistent reservations serialize spending across processes."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from core.config_policy import AutonomyConfig
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

_DAY_SECONDS = 86_400


def _nonnegative_finite(value) -> float:
    """Reject values whose comparisons can silently disable a money cap."""
    if isinstance(value, bool):
        raise ValueError("money amount must be finite and nonnegative")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("money amount must be finite and nonnegative")
    return number


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: Optional[str]


class PolicyGate:
    def __init__(self, max_per_tx_usd: float, audit_sink: Optional[List[dict]] = None,
                 daily_cap_usd: Optional[float] = None,
                 per_venue_daily_cap_usd: Optional[dict] = None,
                 clock: Callable[[], float] = time.time,
                 on_record: Optional[Callable[[dict], None]] = None,
                 cap_resolver: Optional[Callable[[], tuple]] = None):
        self._ceiling = _nonnegative_finite(max_per_tx_usd)
        # 2026-09-18: the two owner caps are LIVE. `cap_resolver` returns
        # ``(max_per_tx_usd, daily_cap_usd)`` as configured right now (see
        # ``core/wallet/config.py::live_caps_resolver``); it is consulted on
        # every check and by the two cap properties, so a preference the owner
        # approved from chat applies at once instead of at the next restart.
        # A leg the resolver cannot answer keeps the constructed value.
        self._cap_resolver = cap_resolver
        # Telemetry hook (audit 2026-07-04): fired with each recorded spend entry so
        # a durable sink can capture money movement. Fail-open — never break record().
        self._on_record = on_record
        self._audit: List[dict] = audit_sink if audit_sink is not None else []
        self._daily_cap: Optional[float] = (
            None if daily_cap_usd is None else _nonnegative_finite(daily_cap_usd)
        )
        # Per-venue rolling-24h caps so one venue can't drain the global budget.
        self._per_venue_cap: dict = {
            str(k).lower(): _nonnegative_finite(v)
            for k, v in (per_venue_daily_cap_usd or {}).items()
        }
        self._now = clock
        # Rebuild the replay-guard set from a pre-loaded (persistent) sink so
        # idempotency survives a restart. No-op for the default empty list.
        self._seen_idempotency: set[str] = {
            e["idempotency_key"] for e in self._audit
            if e.get("idempotency_key")
        }
        # M4 (2026-07-15): per-instance mutex so a caller can make check -> (awaited
        # network spend) -> record ATOMIC. Without it, two concurrent value-moving
        # calls both check() a nearly-exhausted cap at the same rolling-spend, both
        # pass, then both record — clearing past the cap. Created lazily on first
        # `reserve()` so it binds to the running loop (PolicyGate is built
        # synchronously, sometimes with no loop yet). check()/record() are unchanged
        # and remain callable without the lock (legacy callers).
        self._reserve_lock: Optional[asyncio.Lock] = None

    @asynccontextmanager
    async def reserve(self):
        """Serialize a check -> spend -> record critical section per gate instance.

        Usage (the caller keeps calling the unchanged check()/record())::

            async with gate.reserve():
                d = gate.check(...)
                if not d.allowed:
                    return refuse(d.reason)
                result = await do_the_spend(...)
                gate.record(...)

        Holding the lock across the awaited spend is what stops two concurrent
        callers from both passing a nearly-exhausted cap (M4). Persistent sinks additionally hold a shared file lock across this window;
        plain-list test sinks retain process-local serialization.
        """
        if self._reserve_lock is None:
            self._reserve_lock = asyncio.Lock()
        async with self._reserve_lock:
            shared_reserve = getattr(self._audit, "reserve", None)
            if shared_reserve is None:
                yield
            else:
                async with shared_reserve():
                    yield

    def _sync_shared_ledger(self) -> bool:
        """Fold in spends recorded by ANOTHER process before deciding.

        The durable sink is one file shared by every wallet-touching process, but
        it was read once at construction — so a second process (the owner running
        a CLI trade while the daemon trades) judged the rolling-24h cap and the
        replay guard against a snapshot from its own start, and both could clear a
        nearly-exhausted cap. `reserve()` serializes the check→spend→record window
        WITHIN a process; this is what makes the numbers it checks shared.

        A refresh error or an unhealthy durable sink refuses spending. An old
        in-memory view cannot establish the remaining shared cap.
        """
        refresh = getattr(self._audit, "refresh", None)
        if refresh is None:
            return True                 # plain list sink: nothing shared to sync
        try:
            if refresh():
                # New entries may carry keys this process has never seen.
                self._seen_idempotency = {
                    e["idempotency_key"] for e in self._audit
                    if e.get("idempotency_key")
                }
        except Exception as e:
            logger.error("wallet audit refresh failed — refusing spend: %s", e)
            return False
        return getattr(self._audit, "healthy", True) is True

    def _rolling_24h_spend(self, venue: Optional[str] = None) -> float:
        window_start = self._now() - _DAY_SECONDS
        return sum(
            _nonnegative_finite(e.get("amount_usd", 0.0))
            for e in self._audit
            if float(e.get("ts") or 0.0) >= window_start
            and (venue is None or str(e.get("venue", "")).lower() == venue)
        )

    def check(self, *, venue: str, amount_usd: float, idempotency_key: Optional[str]) -> PolicyDecision:
        try:
            amount_usd = _nonnegative_finite(amount_usd)
        except (TypeError, ValueError, OverflowError):
            return PolicyDecision(False, "money amount must be finite and nonnegative")
        # H5: the owner kill-switch is STRUCTURALLY part of the money gate. Every
        # PolicyGate consumer (x402, trading, any future money verb) refuses while
        # halted, regardless of per-call-site discipline. (The probe import is
        # top-level since WS-1 ph4 — core.config_policy is core-tier and light.)
        # Fail CLOSED: if the probe raises, treat as halted rather than opening
        # the gate. At defaults (not halted) this is transparent — the decision
        # below is byte-identical.
        try:
            _halted = AutonomyConfig.autonomy_halted()
        except Exception as e:
            # Distinct reason from the genuine-halt branch below: a broken import
            # or a raising probe is an INFRASTRUCTURE failure, not the owner's
            # kill-switch — don't let an operator mistake "my import is broken"
            # for "I halted autonomy." Still fail CLOSED (money path).
            logger.error(
                "PolicyGate halt probe failed — refusing (fail closed): %s", e,
                exc_info=True,
            )
            return PolicyDecision(
                False,
                "kill-switch probe failed — money paths refused (fail closed)",
            )
        if _halted:
            return PolicyDecision(
                False, "owner kill-switch active — autonomy halted, money movement refused")
        self._refresh_caps()
        if amount_usd > self._ceiling:
            return PolicyDecision(False, f"amount ${amount_usd:.2f} exceeds catastrophic ceiling ${self._ceiling:.2f}")
        try:
            from core.wallet.submission_journal import unresolved
            if unresolved():
                return PolicyDecision(False, "unaccounted wallet submission; reconcile before further spending")
        except Exception:
            return PolicyDecision(False, "wallet submission journal unavailable; spending refused")
        # Both guards below (replay, rolling-24h caps) are computed from the audit
        # ledger, so pick up anything another process appended since we loaded it.
        if not self._sync_shared_ledger():
            return PolicyDecision(False, "wallet audit ledger unavailable or damaged — spending refused")
        try:
            # Validate ALL entries, including timestamps and old rows, before
            # replay/cap decisions. NaN history can silently disable comparisons.
            for entry in self._audit:
                _nonnegative_finite(entry["amount_usd"])
                _nonnegative_finite(entry["ts"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return PolicyDecision(False, "wallet audit ledger contains invalid money history")
        if idempotency_key and idempotency_key in self._seen_idempotency:
            return PolicyDecision(False, f"idempotency key '{idempotency_key}' already used (replay blocked)")
        if self._daily_cap is not None:
            spent = self._rolling_24h_spend()
            if spent + amount_usd > self._daily_cap:
                return PolicyDecision(
                    False,
                    f"daily spend cap ${self._daily_cap:.2f} would be exceeded "
                    f"(trailing-24h ${spent:.2f} + ${amount_usd:.2f})",
                )
        venue_cap = self._per_venue_cap.get(str(venue).lower())
        if venue_cap is not None:
            venue_spent = self._rolling_24h_spend(venue=str(venue).lower())
            if venue_spent + amount_usd > venue_cap:
                return PolicyDecision(
                    False,
                    f"venue '{venue}' daily cap ${venue_cap:.2f} would be exceeded "
                    f"(trailing-24h ${venue_spent:.2f} + ${amount_usd:.2f})",
                )
        return PolicyDecision(True, None)

    def record(self, *, venue: str, action: str, amount_usd: float,
               counterparty: Optional[str], idempotency_key: Optional[str],
               result_ref: Optional[str], chain: Optional[str] = None,
               asset: Optional[str] = None,
               positions: Optional[list] = None,
               submission_ref: Optional[str] = None) -> None:
        """Record one completed spend.

        ``asset`` (2026-09-15) names a NON-FUNGIBLE this transaction moved, as
        ``"erc721:<contract>:<token_id>"``. ``counterparty`` cannot carry it:
        for an NFT transfer the counterparty is the RECIPIENT, and the thing
        that moved needs its own field or the `collectibles` status view has
        nothing to derive from. Additive with a default, so every existing call
        site is unchanged.

        ``positions`` (043 A35) is an optional list of
        ``core.open_positions.PositionDelta`` — the trade's effect on the tracked
        book (a buy opens/increases, a sell reduces/closes). REACH, NOT POLICY:
        this is written AFTER the spend is booked, is fully fail-open, and never
        touches a gate decision or a cap. Only real value-moving swaps pass it;
        every other verb passes nothing and is byte-identical to before. The
        write is tenant-scoped via the ambient exec identity the action batch
        binds (``core/exec_identity.py``) — the same fallback the durable spend
        telemetry uses, since PolicyGate is a shared singleton with no context.
        """
        amount_usd = _nonnegative_finite(amount_usd)
        if idempotency_key:
            self._seen_idempotency.add(idempotency_key)
        entry = {
            "ts": self._now(),
            "venue": str(venue).lower(),
            "action": action,
            "amount_usd": amount_usd,
            "counterparty": counterparty,
            "idempotency_key": idempotency_key,
            "result_ref": result_ref,
            "chain": chain,
            "asset": asset,
        }
        if submission_ref:
            entry["submission_ref"] = submission_ref
        self._audit.append(entry)
        # Release the submission interlock only after a DURABLE cap charge.
        # A plain-list test/embedded sink cannot prove persistence.
        if hasattr(self._audit, 'healthy') and self._audit.healthy is True:
            from core.wallet import submission_journal
            submission_journal.mark_booked(result_ref, amount_usd=amount_usd, venue=venue)
            if submission_ref:
                submission_journal.mark_booked(submission_ref, amount_usd=amount_usd, venue=venue)
        if self._on_record is not None:
            try:
                self._on_record(entry)
            except Exception:
                pass  # fail-open: telemetry must never break a recorded spend
        if positions:
            try:
                from core.exec_identity import current_exec_identity
                from core.open_positions import apply_deltas
                uid, _sid = current_exec_identity()
                if uid:
                    apply_deltas(uid, positions)
            except Exception:
                # Fail-open: the book bookkeeping must never break a recorded
                # spend. The spend above stands regardless.
                logger.debug("position-store write skipped (fail-open)",
                             exc_info=True)

    @property
    def audit_log(self) -> List[dict]:
        return list(self._audit)

    def _refresh_caps(self) -> None:
        """Re-read the owner caps through the resolver, fail-open per leg."""
        if self._cap_resolver is None:
            return
        try:
            per_tx, daily = self._cap_resolver()
        except Exception:
            logger.debug("PolicyGate: cap resolver raised — keeping the "
                         "constructed caps", exc_info=True)
            return
        if per_tx is not None:
            try:
                self._ceiling = _nonnegative_finite(per_tx)
            except (TypeError, ValueError, OverflowError):
                pass
        if daily is None:
            self._daily_cap = None          # resolved: the operator disabled it
        elif isinstance(daily, (int, float)) and not isinstance(daily, bool):
            try:
                self._daily_cap = _nonnegative_finite(daily)
            except (TypeError, ValueError, OverflowError):
                pass
        # any other object = the daily leg was unresolved: keep what we had

    @property
    def daily_cap_usd(self) -> Optional[float]:
        """The rolling-24h ceiling, or None when the operator disabled it.

        Public so a REPORT can name the headroom a spend consumed without
        reaching into the gate's internals. Read-only by construction.
        """
        self._refresh_caps()
        return self._daily_cap

    @property
    def per_tx_cap_usd(self) -> float:
        """The catastrophic per-transaction ceiling (A39: caps need headroom
        context on a finance surface, not just a refusal reason). Public for
        the same reason as :attr:`daily_cap_usd` — read-only by construction.
        """
        self._refresh_caps()
        return self._ceiling

    def rolling_24h_spend_usd(self, venue: Optional[str] = None) -> float:
        """Spend inside the rolling 24h window. See :attr:`daily_cap_usd`."""
        return self._rolling_24h_spend(venue)

    @property
    def has_daily_cap(self) -> bool:
        """True when a rolling-24h aggregate spend cap is configured.

        The autonomous (unattended) spend lane leans on this as its damage bound
        — the per-tx ceiling alone cannot stop a within-ceiling loop draining the
        treasury one ticket at a time. tx_guard refuses the autonomous lane when
        this is False so an operator can't arm unattended trading with no
        aggregate limit.
        """
        self._refresh_caps()
        return self._daily_cap is not None
