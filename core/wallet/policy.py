"""PolicyGate: cheap, backend-independent hygiene for value-moving actions.

NOT a budget engine (the operator asked for 'no limits now'); it enforces ONE
catastrophic per-tx ceiling, idempotency de-dup, and an append-only audit trail.
A future budget engine can extend `check`."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from core.config_policy import AutonomyConfig
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

_DAY_SECONDS = 86_400


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: Optional[str]


class PolicyGate:
    def __init__(self, max_per_tx_usd: float, audit_sink: Optional[List[dict]] = None,
                 daily_cap_usd: Optional[float] = None,
                 per_venue_daily_cap_usd: Optional[dict] = None,
                 clock: Callable[[], float] = time.time,
                 on_record: Optional[Callable[[dict], None]] = None):
        self._ceiling = float(max_per_tx_usd)
        # Telemetry hook (audit 2026-07-04): fired with each recorded spend entry so
        # a durable sink can capture money movement. Fail-open — never break record().
        self._on_record = on_record
        self._audit: List[dict] = audit_sink if audit_sink is not None else []
        self._daily_cap: Optional[float] = (
            None if daily_cap_usd is None else float(daily_cap_usd)
        )
        # Per-venue rolling-24h caps so one venue can't drain the global budget.
        self._per_venue_cap: dict = {
            str(k).lower(): float(v)
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
        callers from both passing a nearly-exhausted cap (M4). Per-process only —
        a shared cross-process ledger is a separate, larger change.
        """
        if self._reserve_lock is None:
            self._reserve_lock = asyncio.Lock()
        async with self._reserve_lock:
            yield

    def _rolling_24h_spend(self, venue: Optional[str] = None) -> float:
        window_start = self._now() - _DAY_SECONDS
        return sum(
            float(e.get("amount_usd") or 0.0)
            for e in self._audit
            if float(e.get("ts") or 0.0) >= window_start
            and (venue is None or e.get("venue") == venue)
        )

    def check(self, *, venue: str, amount_usd: float, idempotency_key: Optional[str]) -> PolicyDecision:
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
        if amount_usd > self._ceiling:
            return PolicyDecision(False, f"amount ${amount_usd:.2f} exceeds catastrophic ceiling ${self._ceiling:.2f}")
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
            venue_spent = self._rolling_24h_spend(venue=venue)
            if venue_spent + amount_usd > venue_cap:
                return PolicyDecision(
                    False,
                    f"venue '{venue}' daily cap ${venue_cap:.2f} would be exceeded "
                    f"(trailing-24h ${venue_spent:.2f} + ${amount_usd:.2f})",
                )
        return PolicyDecision(True, None)

    def record(self, *, venue: str, action: str, amount_usd: float,
               counterparty: Optional[str], idempotency_key: Optional[str],
               result_ref: Optional[str]) -> None:
        if idempotency_key:
            self._seen_idempotency.add(idempotency_key)
        entry = {
            "ts": self._now(),
            "venue": venue,
            "action": action,
            "amount_usd": amount_usd,
            "counterparty": counterparty,
            "idempotency_key": idempotency_key,
            "result_ref": result_ref,
        }
        self._audit.append(entry)
        if self._on_record is not None:
            try:
                self._on_record(entry)
            except Exception:
                pass  # fail-open: telemetry must never break a recorded spend

    @property
    def audit_log(self) -> List[dict]:
        return list(self._audit)
