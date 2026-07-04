"""PolicyGate: cheap, backend-independent hygiene for value-moving actions.

NOT a budget engine (the operator asked for 'no limits now'); it enforces ONE
catastrophic per-tx ceiling, idempotency de-dup, and an append-only audit trail.
A future budget engine can extend `check`."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional

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

    def _rolling_24h_spend(self, venue: Optional[str] = None) -> float:
        window_start = self._now() - _DAY_SECONDS
        return sum(
            float(e.get("amount_usd") or 0.0)
            for e in self._audit
            if float(e.get("ts") or 0.0) >= window_start
            and (venue is None or e.get("venue") == venue)
        )

    def check(self, *, venue: str, amount_usd: float, idempotency_key: Optional[str]) -> PolicyDecision:
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
