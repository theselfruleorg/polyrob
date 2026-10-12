"""Backoff for a failing Telegram long-poll.

Prod 2026-10-09/10: during a Bot API 502 Bad Gateway streak the poll loop
retried on a flat ``asyncio.sleep(1)`` — about 30 getUpdates calls in 45 s
against a server that was down. A failing poll now waits 1, 2, 4, 8, 16, 30 s
(capped), each with jitter, and the first success resets it. Flood control
(``retry_after``) keeps its own exact wait in the harness.
"""
import random
from typing import Callable, Optional

BASE_SEC = 1.0
CAP_SEC = 30.0


def poll_backoff_sec(fail_streak: int, *, base: float = BASE_SEC, cap: float = CAP_SEC,
                     rand: Optional[Callable[[], float]] = None) -> float:
    """Seconds to wait after the ``fail_streak``-th consecutive failure (1-based).

    Exponential, capped, with "equal jitter": a uniform pick in [half, full] of
    the step, so many processes do not retry in lockstep and the wait never
    collapses to zero.
    """
    n = max(1, int(fail_streak or 1))
    step = min(cap, base * (2 ** min(n - 1, 16)))
    return step / 2 + (step / 2) * (rand or random.random)()
