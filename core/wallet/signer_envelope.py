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
reading). A signer that has never answered clamps every spend cap to zero, including
in shadow mode.
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


def _enabled() -> bool:
    if os.environ.get(SIGNER_PROCESS_ENV):
        return False
    try:
        from core.signer import MODE_LOCAL, signer_mode
        return signer_mode() != MODE_LOCAL
    except Exception:
        return False


def signer_caps() -> Optional[dict]:
    """``{leg: value}`` for :data:`LEGS` from the signer's ``ping``, cached
    (the gate asks on every spend check). ``None`` in local mode, inside the
    signer; before the first successful ping enabled signers impose zero caps.
    After one good answer a failed ping returns the last good caps."""
    if not _enabled():
        return None
    hit, caps = _cached(time.monotonic())
    if hit:
        return caps
    with _refresh:
        hit, caps = _cached(time.monotonic())   # another thread refreshed it
        if hit:
            return caps
        fresh = _ping()
        now = time.monotonic()
        with _lock:
            if fresh is not None:
                _cache.update(at=now, ttl=_TTL_SEC, caps=fresh, good=fresh)
            else:
                _cache.update(at=now, ttl=_FAIL_TTL_SEC,
                              caps=_cache.get("good") or dict.fromkeys(LEGS, 0.0))
            return _cache["caps"]


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
    zero clamp :func:`signer_caps` imposes on a silent signer: read as a cap,
    that zero told the owner to raise ``signer.toml`` when the fix is to start
    the signer."""
    if signer_caps() is None:
        return None
    with _lock:
        return _cache.get("good")


def signer_unanswered() -> bool:
    """A signer is installed but has never answered: every spend cap is zero."""
    return signer_caps() is not None and reported_caps() is None


#: What a readout says instead of a "bound by the signer" line when the
#: signer has never answered.
UNANSWERED_TEXT = ("the wallet signer has not answered, so every spend cap is held at "
                   "$0 until it does. Check it: `sudo systemctl status polyrob-signer`")


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
