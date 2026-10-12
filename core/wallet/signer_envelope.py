"""The signer's hard caps ARE the envelope (owner decision 2026-10-06).

``polyrob-signer`` enforces its own caps from ``/etc/polyrob/signer.toml``
(root-owned). Before this module the in-process gate resolved its caps from the
env + the owner's ``budget.wallet_*`` prefs alone, so one concept — "the most a
transaction / a day may move" — lived in two stores with no sync. The owner
raised the per-tx pref to $3,000 and the env daily cap to $7,000; the signer
stayed at $120/$3,000 and every digest reported the "cap mismatch".

The model now: when a signer is installed (``WALLET_SIGNER=shadow|remote``) and
answers, the gate's effective caps are ``min(configured, signer)``. The env +
pref values are the agent's OPERATING caps inside that envelope; a raise from
chat can never pass it. Raising the envelope is one root edit on the box. The
signer still never reads the agent-writable prefs — the agent reads the
signer, never the reverse.

This applies in ``shadow`` too: shadow still signs locally, but a send above
the signer's cap is refused by the agent's own gate (the owner chose the signer
as the envelope; a shadow week with a lower signer cap is not a free pass).

A signer that stops answering keeps its LAST GOOD caps (tighter is the safe
reading). A signer that has never answered:

* ``remote`` — clamps every spend cap to zero: the signer holds the key, so
  nothing can sign without it anyway;
* ``shadow`` — imposes no envelope (D5, owner 2026-10-10): shadow signs
  locally, so the agent's own caps apply, and every readout names the silent
  signer (:func:`unanswered_text`). A signer unit that is down at boot no
  longer blocks every spend until a root fix.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

SIGNER_TOML = "/etc/polyrob/signer.toml"

#: Set by ``core.signer.runtime.prepare_process``: the signer process itself
#: resolves caps through the same code and must never ping its own socket.
SIGNER_PROCESS_ENV = "POLYROB_SIGNER_PROCESS"

#: Every hard limit the signer enforces that the agent also applies. The agent
#: clamps each of its own values to these; a leg the signer does not report
#: (an older signer) is no clamp.
LEGS = ("per_tx_usd", "daily_usd", "x402_per_payment_usd", "x402_max_window_sec")

_TTL_SEC = 60.0          # a good answer is re-checked once a minute
_FAIL_TTL_SEC = 5.0      # a failed ping is retried soon
#: A local Unix socket answers in milliseconds; this bounds how long a wedged
#: signer can hold the caller (the gate runs inside async tool code).
_PING_TIMEOUT_SEC = 0.5
_lock = threading.Lock()          # guards _cache
_refresh = threading.Lock()       # one ping at a time (single flight)
_cache: dict = {}   # {"at": float, "ttl": float, "caps": Optional[dict], "good": Optional[dict]}


def _num(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out >= 0 else None


def _mode() -> Optional[str]:
    """``shadow``/``remote`` when an installed signer applies here, else None."""
    if os.environ.get(SIGNER_PROCESS_ENV):
        return None
    try:
        from core.signer import MODE_LOCAL, signer_mode
        mode = signer_mode()
        return None if mode == MODE_LOCAL else mode
    except Exception:
        return None


def _enabled() -> bool:
    return _mode() is not None


def signer_caps() -> Optional[dict]:
    """``{leg: value}`` for :data:`LEGS` from the signer's ``ping``, cached
    (the gate asks on every spend check). ``None`` in local mode, inside the
    signer, and for a ``shadow`` signer that has never answered (D5: the local
    caps apply). Before the first successful ping a ``remote`` signer imposes
    zero caps. After one good answer a failed ping returns the last good caps."""
    mode = _mode()
    if mode is None:
        return None
    hit, caps = _cached(time.monotonic())
    if not hit:
        with _refresh:
            hit, caps = _cached(time.monotonic())   # another thread refreshed it
            if not hit:
                fresh = _ping()
                now = time.monotonic()
                with _lock:
                    if fresh is not None:
                        _cache.update(at=now, ttl=_TTL_SEC, caps=fresh, good=fresh)
                    else:
                        _cache.update(at=now, ttl=_FAIL_TTL_SEC,
                                      caps=_cache.get("good") or dict.fromkeys(LEGS, 0.0))
                    caps = _cache["caps"]
    if mode != "remote" and _never_answered():
        return None
    return caps


def _never_answered() -> bool:
    with _lock:
        return bool(_cache) and _cache.get("good") is None


def _cached(now: float):
    with _lock:
        if _cache and now - _cache["at"] < _cache["ttl"]:
            return True, _cache["caps"]
    return False, None


def _ping() -> Optional[dict]:
    try:
        from core.signer.client import SignerClient
        pong = SignerClient(timeout=_PING_TIMEOUT_SEC).call("ping")
    except Exception:
        logger.debug("signer ping failed; keeping the last good caps", exc_info=True)
        return None
    raw = pong.get("caps") or {}
    return {leg: _num(raw.get(leg)) for leg in LEGS}


def reported_caps() -> Optional[dict]:
    """For READOUTS: the caps the signer last actually reported, or None (local
    mode, inside the signer, or a signer that has never answered). Never the
    zero clamp :func:`signer_caps` imposes on a silent remote signer: read as a
    cap, that zero told the owner to raise ``signer.toml`` when the fix is to
    start the signer."""
    if not _enabled():
        return None
    signer_caps()           # refresh the cache when it is due
    with _lock:
        return _cache.get("good")


def signer_unanswered() -> bool:
    """A signer is installed (``shadow``/``remote``) but has never answered."""
    return _enabled() and reported_caps() is None


def spend_held_by_signer() -> bool:
    """A ``remote`` signer that never answered holds every spend at $0 (the
    shadow fallback does not: D5)."""
    return signer_unanswered() and _mode() == "remote"


#: What a readout says instead of a "bound by the signer" line when a REMOTE
#: signer has never answered.
UNANSWERED_TEXT = ("the wallet signer has not answered, so every spend cap is held at "
                   "$0 until it does. Check it: `sudo systemctl status polyrob-signer`")
#: The same for a SHADOW signer (D5): the local caps apply meanwhile.
SHADOW_UNANSWERED_TEXT = ("the wallet signer (shadow) has not answered, so the agent's own "
                          "caps apply until it does. Check it: "
                          "`sudo systemctl status polyrob-signer`")


def unanswered_text() -> str:
    """The owner-facing notice for a signer that has never answered, by mode."""
    return UNANSWERED_TEXT if _mode() == "remote" else SHADOW_UNANSWERED_TEXT


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def envelope(leg: str) -> Optional[float]:
    """The signer's hard cap for *leg* (one of :data:`LEGS`), or None."""
    caps = signer_caps()
    return None if caps is None else caps.get(leg)


def clamp(value: Optional[float], leg: str) -> Optional[float]:
    """``min(value, signer)``; ``value`` None (disabled daily) takes the signer's."""
    cap = envelope(leg)
    if cap is None:
        return value
    if value is None:
        return cap
    return min(float(value), cap)


@dataclass(frozen=True)
class BoundLeg:
    """One cap leg where the configured value is above the signer's envelope.
    Not a fault: the gate already enforces the lower number. It only tells the
    owner that a raise they made does not apply past the signer."""
    leg: str                 # "per_tx" | "daily"
    configured: float        # math.inf = the operator disabled that leg
    signer: float


_PREF_VERB = {"per_tx": "per-tx", "daily": "daily"}
_KEY = {"per_tx": "per_tx_usd", "daily": "daily_usd"}


def bound_legs(signer: Mapping, configured: tuple) -> list:
    """The legs where *configured* ``(per_tx, daily)`` exceeds *signer*. Pure.
    A daily of None = disabled = unlimited; an unknown side is never "bound"."""
    per_tx, daily = configured
    out = []
    for leg, conf in (("per_tx", per_tx), ("daily", daily)):
        sig = _num(signer.get(_KEY[leg]))
        conf_v = math.inf if (leg == "daily" and conf is None) else _num(conf)
        if sig is not None and conf_v is not None and conf_v > sig + 1e-9:
            out.append(BoundLeg(leg, conf_v, sig))
    return out


def _usd(value: float) -> str:
    return "unlimited" if math.isinf(value) else f"${value:,.2f}"


def bound_text(b: BoundLeg) -> str:
    return (f"{_PREF_VERB[b.leg]} cap: {_usd(b.signer)} (the signer's hard cap; the agent "
            f"setting of {_usd(b.configured)} does not apply above it). To raise it, as root "
            f"set `{_KEY[b.leg]}` under `[caps]` in {SIGNER_TOML}, then "
            f"`sudo systemctl restart polyrob-signer`")
