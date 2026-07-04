"""Per-(surface,dest) token bucket pacing the outbound dispatcher. In-memory: the durable
queue is the at-least-once layer; this just keeps the worker under platform throughput
(Telegram ~30 msg/s global, WhatsApp tiered, etc.)."""
from typing import Dict, Tuple


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self.rate = float(rate_per_sec)
        self.burst = float(burst)
        self._state: Dict[str, Tuple[float, float]] = {}  # key -> (tokens, last_ts)

    def take(self, key: str, *, now: float) -> Tuple[bool, float]:
        tokens, last = self._state.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens >= 1.0:
            self._state[key] = (tokens - 1.0, now)
            return True, 0.0
        deficit = 1.0 - tokens
        self._state[key] = (tokens, now)
        return False, deficit / self.rate if self.rate > 0 else 1.0
