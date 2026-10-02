"""Bot-pair loop guard (064 F4): two bots must not talk each other into a loop.

Every inbound whose sender is itself a bot (``InboundMessage.sender_is_bot``)
counts against its (surface, peer bot) pair — our side is the one bot this
process runs on that surface. Past ``events`` in ``window_s`` the pair goes
silent for ``cooldown_s``. The numbers are the surface's catalog row
(``SurfaceSpec.bot_pair_limit``, default 20 / 60 s / 60 s — the OpenClaw
number), not an env flag.

In memory on purpose: a loop is a burst, and a restart that forgets the count
also breaks the loop. ``core/surfaces/room_caps.py`` keeps the durable,
per-ROOM bot cap for groups; this one covers every surface and DMs too.
"""
import threading
import time
from collections import deque
from typing import Dict, Optional, Tuple

_lock = threading.Lock()
_events: Dict[Tuple[str, str], deque] = {}
_cooldown_until: Dict[Tuple[str, str], float] = {}

#: Bot accounts are free to mint, so the maps are bounded: idle pairs are swept,
#: and past the cap the least recently active pairs are forgotten (forgetting a
#: pair only restarts its count — it never drops a message).
_MAX_PAIRS = 10_000
_SWEEP_EVERY = 256
_calls = 0


def _sweep(now: float, window_s: int) -> None:
    for key in [k for k, q in _events.items() if not q or q[-1] <= now - window_s]:
        if _cooldown_until.get(key, 0.0) <= now:
            _events.pop(key, None)
            _cooldown_until.pop(key, None)
    for key in [k for k, t in _cooldown_until.items() if t <= now and k not in _events]:
        _cooldown_until.pop(key, None)
    if len(_events) > _MAX_PAIRS:
        by_age = sorted(_events, key=lambda k: _events[k][-1] if _events[k] else 0.0)
        for key in by_age[:len(_events) - _MAX_PAIRS]:
            _events.pop(key, None)
            _cooldown_until.pop(key, None)


def _limits(surface_id: str) -> Tuple[int, int, int]:
    from core.surfaces.catalog import get
    spec = get(surface_id)
    return spec.bot_pair_limit if spec is not None else (20, 60, 60)


def allow(surface_id: str, peer_id: str, *, now: Optional[float] = None) -> bool:
    """Count one bot event for the pair; False = drop it (the pair is looping)."""
    now = time.time() if now is None else now
    key = (surface_id or "", str(peer_id or ""))
    events, window_s, cooldown_s = _limits(surface_id)
    global _calls
    with _lock:
        _calls += 1
        if _calls % _SWEEP_EVERY == 0 or len(_events) > _MAX_PAIRS:
            _sweep(now, window_s)
        if _cooldown_until.get(key, 0.0) > now:
            return False
        q = _events.setdefault(key, deque())
        while q and q[0] <= now - window_s:
            q.popleft()
        if len(q) >= events:
            _cooldown_until[key] = now + cooldown_s
            q.clear()
            return False
        q.append(now)
        return True


def reset() -> None:
    """TEST-ONLY seam."""
    with _lock:
        _events.clear()
        _cooldown_until.clear()
