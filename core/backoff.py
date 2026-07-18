"""Shared exponential-backoff-with-jitter delay (P4 finalization).

The same `base * mult**n`, jitter `random.uniform(0.8, 1.2)`, cap formula was
open-coded in `agents/task/robust_parse_config.py` once and in
`agents/task/agent/core/error_recovery.py` twice (with a bare `120` cap literal
that could silently drift from the config-driven one). Centralizing the formula
keeps the jitter range + exponential structure in one place; each caller passes
its own base/multiplier/cap.

Note the two historical call sites cap at different points relative to jitter, so
that choice is a parameter (``cap_after_jitter``) — preserved exactly, not changed.
"""
import random


def jittered_exponential_delay(
    base: float,
    failures: int,
    *,
    multiplier: float = 2.0,
    cap: float,
    jitter_range: tuple = (0.8, 1.2),
    cap_after_jitter: bool = False,
) -> float:
    """Return an exponential-backoff delay with anti-thundering-herd jitter.

    ``cap_after_jitter=False`` (default, robust_parse style): ``min(base*mult**n, cap)``
    is jittered, so the result may slightly exceed ``cap`` (up to ``cap * hi``).
    ``cap_after_jitter=True`` (error_recovery style): the jittered delay is then
    capped, so the result NEVER exceeds ``cap``.
    """
    delay = base * (multiplier ** max(0, failures))
    lo, hi = jitter_range
    jitter = random.uniform(lo, hi)
    if cap_after_jitter:
        return min(delay * jitter, cap)
    return min(delay, cap) * jitter
