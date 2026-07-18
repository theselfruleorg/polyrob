"""Canonical in-process rate-limiting primitives (F-1, 2026-07-17).

Every rate limiter in the tree is a configured instance of (or a thin wrapper
delegating to) one of the three shapes below:

- ``SlidingWindowLimiter`` — N calls per rolling window, per key. Generalized from
  the WS-B3 MCP exec limiter (``tools/mcp/rate_limit.py`` is now a back-compat
  shim). Used by: MCP tool execution, user MCP server admin, the public x402
  invoice endpoints, the webview connection/event throttles, and
  ``utils/rate_limit_manager.py``.
- ``TokenBucket`` — burst + steady refill, per key (moved here from
  ``core/surfaces/rate_bucket.py``, which re-exports). Used by: the outbound
  surface dispatcher and the api middleware's burst gate.
- ``FixedWindowCounter`` — N events per fixed window that starts at a key's first
  touch and resets ``window_seconds`` later. Extracted from the api middleware's
  legacy minute/hour counters.

All three are pure and clock-injectable: ``TokenBucket``/``FixedWindowCounter``
take ``now`` per call; ``SlidingWindowLimiter`` takes a ``time_fn`` (``None`` =
live ``time.time()`` lookup at call time, so tests may patch ``time.time``).
Each keeps its key space bounded via an amortized idle-key sweep — a key whose
window has expired (or whose bucket is fully refilled) is semantically identical
to a fresh key, so evict-then-recreate never changes a decision (WS-4 precedent).

The ONE deliberate non-consolidation: ``surfaces/telegram/rate_limit.py::
TelegramRateLimiter`` is not a request-budget limiter — it only replays penalties
Telegram already issued via RetryAfter (grammY philosophy: never pre-throttle),
so there is no budget shape here for it to configure.

A shrink-only name ratchet (``tests/test_rate_limiter_ratchet.py``) keeps new
forks from appearing outside this module.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Hashable, List, Optional, Tuple


class SlidingWindowLimiter:
    """N calls per rolling ``window_seconds``, per key. Denied calls are NOT
    recorded. ``max_calls``/``window`` may be overridden per call (used by
    ``RateLimitManager`` whose limits vary by operation); the idle-key sweep
    prunes against the largest window ever requested, so a conservative window
    override never loses live state to the sweep."""

    _SWEEP_INTERVAL = 300.0

    def __init__(
        self,
        max_calls: int = 20,
        window_seconds: float = 60,
        time_fn: Optional[Callable[[], float]] = None,
        max_keys: Optional[int] = None,
    ):
        self.max_calls = max_calls
        self.window_seconds = float(window_seconds)
        self._time_fn = time_fn
        self._max_keys = max_keys
        self._calls: Dict[Hashable, List[float]] = {}
        self._last_sweep: Optional[float] = None
        self._sweep_window = self.window_seconds

    def _now(self) -> float:
        return self._time_fn() if self._time_fn is not None else time.time()

    def _effective(self, max_calls: Optional[int], window: Optional[float]) -> Tuple[int, float]:
        win = self.window_seconds if window is None else float(window)
        if win > self._sweep_window:
            self._sweep_window = win
        return (self.max_calls if max_calls is None else max_calls), win

    def _prune(self, key: Hashable, now: float, window: float) -> List[float]:
        """Drop out-of-window timestamps for ``key``. Never creates an entry."""
        prev = self._calls.get(key)
        if prev is None:
            return []
        cutoff = now - window
        kept = [t for t in prev if t > cutoff]
        self._calls[key] = kept
        return kept

    def _maybe_sweep(self, now: float) -> None:
        if self._last_sweep is None:
            self._last_sweep = now
            return
        if now - self._last_sweep < self._SWEEP_INTERVAL:
            return
        self._last_sweep = now
        cutoff = now - self._sweep_window
        stale = [k for k, ts in self._calls.items() if not ts or ts[-1] <= cutoff]
        for k in stale:
            del self._calls[k]

    def check(self, key: Hashable, *, max_calls: Optional[int] = None,
              window: Optional[float] = None) -> bool:
        """Return True and record the call if under the limit, else False."""
        now = self._now()
        self._maybe_sweep(now)
        limit, win = self._effective(max_calls, window)
        kept = self._prune(key, now, win)
        # (Re)insert at the MRU end so max_keys eviction hits the least recently
        # CHECKED key — a denied-but-active key must stay resident, or eviction
        # would hand it a fresh budget.
        self._calls.pop(key, None)
        self._calls[key] = kept
        allowed = len(kept) < limit
        if allowed:
            kept.append(now)
        if self._max_keys is not None:
            while len(self._calls) > self._max_keys:
                del self._calls[next(iter(self._calls))]
        return allowed

    def remaining(self, key: Hashable, *, max_calls: Optional[int] = None,
                  window: Optional[float] = None) -> int:
        """Slots left in the window. Read-only (never consumes)."""
        limit, win = self._effective(max_calls, window)
        kept = self._prune(key, self._now(), win)
        return max(0, limit - len(kept))

    def retry_after(self, key: Hashable, *, max_calls: Optional[int] = None,
                    window: Optional[float] = None) -> float:
        """Seconds until the next slot frees (0 if a slot is available now)."""
        now = self._now()
        limit, win = self._effective(max_calls, window)
        kept = self._prune(key, now, win)
        if len(kept) < limit:
            return 0.0
        return max(0.0, (min(kept) + win) - now)

    def oldest(self, key: Hashable, *, window: Optional[float] = None) -> Optional[float]:
        """Oldest in-window timestamp for ``key``, or None."""
        _, win = self._effective(None, window)
        kept = self._prune(key, self._now(), win)
        return min(kept) if kept else None

    def keys(self) -> list:
        """Keys with at least one recorded in-window call (as of last prune)."""
        return [k for k, ts in self._calls.items() if ts]


class TokenBucket:
    """Per-key token bucket: ``burst`` capacity refilled at ``rate_per_sec``.
    In-memory pacing only — durability is the caller's concern (e.g. the outbound
    dispatcher's queue is the at-least-once layer)."""

    _SWEEP_INTERVAL = 60.0  # amortize the idle-key prune (seconds of injected `now`)

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self.rate = float(rate_per_sec)
        self.burst = float(burst)
        self._state: Dict[str, Tuple[float, float]] = {}  # key -> (tokens, last_ts)
        self._last_sweep: Optional[float] = None

    def _sweep(self, now: float) -> None:
        """WS-4: drop keys idle long enough to be fully refilled — a full bucket at
        rest is byte-equivalent to a fresh one, so evict-then-recreate preserves
        semantics exactly while keeping ``_state`` bounded to active keys."""
        if self.rate <= 0:
            return
        full_after = self.burst / self.rate
        stale = [k for k, (_, last) in self._state.items() if now - last >= full_after]
        for k in stale:
            del self._state[k]

    def peek(self, key: str, *, now: float) -> Tuple[bool, float]:
        """Refill and report availability WITHOUT consuming. Returns
        ``(available, seconds_until_a_token_if_not)``."""
        if self._last_sweep is None:
            self._last_sweep = now
        elif now - self._last_sweep >= self._SWEEP_INTERVAL:
            self._sweep(now)
            self._last_sweep = now
        tokens, last = self._state.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        self._state[key] = (tokens, now)
        if tokens >= 1.0:
            return True, 0.0
        deficit = 1.0 - tokens
        return False, deficit / self.rate if self.rate > 0 else 1.0

    def consume(self, key: str, *, now: float) -> None:
        """Consume one token (call after a successful ``peek``)."""
        tokens, last = self._state.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        self._state[key] = (max(0.0, tokens - 1.0), now)

    def take(self, key: str, *, now: float) -> Tuple[bool, float]:
        """Consume a token if available: ``(True, 0.0)`` or ``(False, wait_sec)``."""
        ok, wait = self.peek(key, now=now)
        if ok:
            self.consume(key, now=now)
            return True, 0.0
        return False, wait


class FixedWindowCounter:
    """Per-key fixed-window counter: at most ``limit`` events per window, where a
    key's window starts at its first touch and resets ``window_seconds`` later
    (the api middleware's legacy minute/hour counter semantics — NOT sliding)."""

    _SWEEP_INTERVAL = 300.0

    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window = float(window_seconds)
        self._state: Dict[Hashable, Tuple[int, float]] = {}  # key -> (count, window_start)
        self._last_sweep: Optional[float] = None

    def _roll(self, key: Hashable, now: float) -> Tuple[int, float]:
        count, start = self._state.get(key, (0, now))
        if now - start >= self.window:
            count, start = 0, now
        return count, start

    def _maybe_sweep(self, now: float) -> None:
        if self._last_sweep is None:
            self._last_sweep = now
            return
        if now - self._last_sweep < self._SWEEP_INTERVAL:
            return
        self._last_sweep = now
        stale = [k for k, (_, start) in self._state.items() if now - start >= self.window]
        for k in stale:
            del self._state[k]

    def peek(self, key: Hashable, *, now: float) -> bool:
        """True if another event fits in the key's current window (no count)."""
        self._maybe_sweep(now)
        count, start = self._roll(key, now)
        self._state[key] = (count, start)
        return count < self.limit

    def increment(self, key: Hashable, *, now: float) -> None:
        self._maybe_sweep(now)
        count, start = self._roll(key, now)
        self._state[key] = (count + 1, start)

    def remaining(self, key: Hashable, *, now: float) -> int:
        count, _ = self._roll(key, now)
        return max(0, self.limit - count)

    def seconds_until_reset(self, key: Hashable, *, now: float) -> float:
        _, start = self._roll(key, now)
        return max(0.0, (start + self.window) - now)
