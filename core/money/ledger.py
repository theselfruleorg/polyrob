"""SpendLedger: cheap, backend-independent hygiene for value-moving actions.

Enforces finite per-transaction and rolling caps, replay protection, and a
durable audit trail. Persistent reservations serialize spending across processes.

067 P1b: moved down from ``core/wallet/policy.py`` (the class was ``PolicyGate``;
that module keeps ``PolicyGate = SpendLedger``). One daily cap across several
rails can only live below all of them. The cap VALUES come from the caller's
``cap_resolver`` (the wallet side resolves it through
``core.money.hooks.cap_resolver``); the kernel does not know where they come
from.

067 P5a: the two rail edges are HOOKS (``core.money.hooks``), no import of
``core.wallet`` or ``core.open_positions``: the submission-journal interlock in
:meth:`SpendLedger.check`/:meth:`record` (an unreconciled broadcast refuses
every spend; an absent or raising journal refuses too — fail CLOSED) and the
position book write in :meth:`record` (absent or raising = skipped — fail
open). The wallet registers both when ``core.wallet`` is imported.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager, contextmanager
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from core.config_policy import AutonomyConfig
from core.money import hooks as _hooks
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

_DAY_SECONDS = 86_400

#: The pause probe :meth:`SpendLedger.check` reads, when a caller scoped one.
#: ``tx_guard`` scopes its OWN ``halted_fn`` around its ``gate.check`` so the two
#: pause reads cannot disagree: an owner-direct send (the pause does not bind the
#: owner) passes both, and the remote signer's gate reads the signer's own pause
#: store — before this the ledger always read the agent process's pause record,
#: whatever the guard had been told (owner-UX review A2, 2026-09-26).
_PAUSE_PROBE: "ContextVar[Optional[Callable[[], bool]]]" = ContextVar(
    "spend_ledger_pause_probe", default=None)


@contextmanager
def pause_probe(fn: Callable[[], bool]):
    """Scope the pause probe :meth:`SpendLedger.check` reads to *fn*, this call only."""
    token = _PAUSE_PROBE.set(fn)
    try:
        yield
    finally:
        _PAUSE_PROBE.reset(token)


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
    #: True only for a CAP refusal (ceiling, rolling daily cap, venue cap) — what an
    #: owner grant can lift. A reader decides on this, never on the reason text.
    cap_exceeded: bool = False


class SpendLedger:
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
        # The lane the guard authorized a spend on, keyed by its idempotency key
        # (``note_lane``), consumed by :meth:`record`. Keyed rather than ambient
        # because several verbs run the guard in a worker thread. Bounded: a
        # spend authorized and never booked must not grow this forever.
        self._lanes: "OrderedDict[str, str]" = OrderedDict()
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

    def _frees_at_note(self, amount_usd: float) -> str:
        """``"; enough frees at HH:MM UTC"`` — when the rolling window will have
        dropped enough spend for *amount_usd* to fit, or ``""`` when it never
        will (the amount alone is above the cap) or cannot be computed. Text
        only: the decision is made by the caller. On 2026-09-26 the agent told
        the owner his headroom came back "this morning"; the ledger knows."""
        try:
            cap = float(self._daily_cap)
            if amount_usd > cap:
                return ""
            now = self._now()
            live = sorted(
                (float(e.get("ts") or 0.0), _nonnegative_finite(e.get("amount_usd", 0.0)))
                for e in self._audit
                if float(e.get("ts") or 0.0) >= now - _DAY_SECONDS)
            spent = sum(usd for _, usd in live)
            for ts, usd in live:
                spent -= usd
                if spent + amount_usd <= cap:
                    import datetime as _dt
                    at = _dt.datetime.fromtimestamp(ts + _DAY_SECONDS, tz=_dt.timezone.utc)
                    return f"; enough frees at {at:%Y-%m-%d %H:%M} UTC"
        except Exception:
            return ""
        return ""

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
            probe = _PAUSE_PROBE.get()
            _halted = bool(probe()) if probe is not None else AutonomyConfig.autonomy_halted()
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
                False, "the owner paused autonomous work — money movement refused "
                       "(the owner lifts it with /resume)")
        self._refresh_caps()
        if amount_usd > self._ceiling:
            return PolicyDecision(False, f"amount ${amount_usd:.2f} exceeds catastrophic ceiling ${self._ceiling:.2f}",
                                  cap_exceeded=True)
        try:
            journal = _hooks.submission_journal()
            if journal is None:
                # No rail registered the interlock: the same refusal as a
                # journal that cannot be read (fail closed).
                return PolicyDecision(False, "wallet submission journal unavailable; spending refused")
            if journal.unresolved():
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
                    f"(trailing-24h ${spent:.2f} + ${amount_usd:.2f})"
                    + self._frees_at_note(amount_usd),
                    cap_exceeded=True,
                )
        venue_cap = self._per_venue_cap.get(str(venue).lower())
        if venue_cap is not None:
            venue_spent = self._rolling_24h_spend(venue=str(venue).lower())
            if venue_spent + amount_usd > venue_cap:
                return PolicyDecision(
                    False,
                    f"venue '{venue}' daily cap ${venue_cap:.2f} would be exceeded "
                    f"(trailing-24h ${venue_spent:.2f} + ${amount_usd:.2f})",
                    cap_exceeded=True,
                )
        return PolicyDecision(True, None)

    #: How many un-booked lane notes :meth:`note_lane` keeps.
    _LANE_NOTES_MAX = 256

    def note_lane(self, idempotency_key: Optional[str], lane: Optional[str]) -> None:
        """Remember the lane the guard authorized *idempotency_key* on, for the
        :meth:`record` that books it. Text for the audit trail only — no cap and
        no decision reads it. The pause alarm (``core/status_snapshot.py``) does:
        an owner-direct spend during a pause is the owner acting, not a breach."""
        if not idempotency_key or not lane:
            return
        self._lanes[str(idempotency_key)] = str(lane)
        self._lanes.move_to_end(str(idempotency_key))
        while len(self._lanes) > self._LANE_NOTES_MAX:
            self._lanes.popitem(last=False)

    def record(self, *, venue: str, action: str, amount_usd: float,
               counterparty: Optional[str], idempotency_key: Optional[str],
               result_ref: Optional[str], chain: Optional[str] = None,
               asset: Optional[str] = None,
               positions: Optional[list] = None,
               submission_ref: Optional[str] = None,
               account: Optional[str] = None,
               native_raw: Optional[int] = None) -> None:
        """Record one completed spend.

        ``native_raw`` (core handoff W6, 090 R4) stamps the native wei a v4
        liquidity add sent, so the LP program caps (``LP_ETH_CAP``,
        ``LP_ETH_DAILY_CAP``) sum a measured figure from this durable ledger.
        ``None`` (default) writes byte-identical entries to before.

        ``account`` (050 §7.4) names the HOLDER when it is not the treasury — a
        token-bound ERC-6551 account. It is stamped on the audit entry and on every
        position leg, so an account's trade lands in that account's book, never the
        treasury's. ``None`` (default) writes byte-identical entries to before.
        The caps are NOT split by account: one operator key, one daily bound.

        ``asset`` (2026-09-15) names a NON-FUNGIBLE this transaction moved, as
        ``"erc721:<contract>:<token_id>"``. ``counterparty`` cannot carry it:
        for an NFT transfer the counterparty is the RECIPIENT, and the thing
        that moved needs its own field or the `collectibles` status view has
        nothing to derive from. Additive with a default, so every existing call
        site is unchanged.

        ``positions`` (043 A35) is an optional list of position deltas
        (``core.open_positions.PositionDelta``, applied through the
        ``core.money.hooks`` position book) — the trade's effect on the tracked
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
        lane = self._lanes.pop(str(idempotency_key), None) if idempotency_key else None
        if lane:
            entry["lane"] = lane
        if account:
            entry["account"] = str(account).lower()
        if native_raw is not None:
            entry["native_raw"] = int(native_raw)
        self._audit.append(entry)
        # Release the submission interlock only after a DURABLE cap charge.
        # A plain-list test/embedded sink cannot prove persistence.
        if hasattr(self._audit, 'healthy') and self._audit.healthy is True:
            submission_journal = _hooks.submission_journal()
            if submission_journal is None:
                raise RuntimeError("wallet submission journal unavailable; "
                                   "the booked spend cannot release its interlock")
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
                apply_deltas = _hooks.position_book()
                uid, _sid = current_exec_identity()
                if uid and apply_deltas is not None:
                    if account:
                        import dataclasses
                        positions = [dataclasses.replace(p, account=str(account).lower())
                                     for p in positions]
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
