"""The signer's hard caps vs the in-process gate's caps (068 G7b).

``polyrob-signer`` enforces its own caps from ``/etc/polyrob/signer.toml``
(root-owned, generated ONCE from the caps in use at install time — 066 D6). The
in-process gate re-reads the owner's ``budget.wallet_*`` preferences live. When
the owner raises a preference after the signer was provisioned, the two drift:
on prod (2026-09-24/25) every $134.54 buyback cleared the gate ($300 pref) and
was refused in shadow by the signer ("exceeds catastrophic ceiling $120.00"). In
shadow that is a log line; after a cut-over to ``WALLET_SIGNER=remote`` it is a
refusal of every such trade.

The signer must NOT read the agent-writable preferences — its independence is
the point. So this module only MEASURES the drift and names the two levers; it
changes nothing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

SIGNER_TOML = "/etc/polyrob/signer.toml"


@dataclass(frozen=True)
class CapDrift:
    """One cap leg. ``signer``/``gate`` are None when that side is UNKNOWN;
    ``gate`` is ``math.inf`` when the operator DISABLED that leg (068 B12: an
    unlimited gate over a finite signer is drift, not "nothing to compare")."""
    leg: str                 # "per_tx" | "daily"
    signer: Optional[float]
    gate: Optional[float]

    @property
    def drifted(self) -> bool:
        """The gate allows MORE than the signer will sign. The reverse (the
        signer is wider) is harmless: the gate refuses first."""
        if self.signer is None or self.gate is None:
            return False
        return self.gate > self.signer + 1e-9


def _num(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def cap_drift(signer_caps: Mapping, gate_caps: Tuple[Optional[float], Optional[float]]):
    """Compare the signer's ``{"per_tx_usd", "daily_usd"}`` (its ``ping``
    answer) with the gate's ``(per_tx, daily)``. Pure."""
    per_tx, daily = gate_caps
    # The gate's daily leg has THREE states (core.wallet.config.live_caps_resolver):
    # a number, None = the operator disabled it (unlimited), and the
    # _UNRESOLVED sentinel = could not be read (unknown). Only the last is unknown.
    from core.wallet.config import _UNRESOLVED
    if daily is _UNRESOLVED:
        gate_daily = None
    elif daily is None:
        gate_daily = math.inf
    else:
        gate_daily = _num(daily)
    return (CapDrift("per_tx", _num(signer_caps.get("per_tx_usd")), _num(per_tx)),
            CapDrift("daily", _num(signer_caps.get("daily_usd")), gate_daily))


def gate_caps_now(env: Optional[Mapping[str, str]] = None, *,
                  user_id: Optional[str] = None, home_dir=None):
    """``(per_tx, daily)`` as the in-process gate resolves them RIGHT NOW —
    the same resolver the gate asks on every check."""
    from core.wallet.config import live_caps_resolver
    return live_caps_resolver(env, user_id=user_id, home_dir=home_dir)()


_KEY = {"per_tx": "per_tx_usd", "daily": "daily_usd"}
_PREF_VERB = {"per_tx": "per-tx", "daily": "daily"}


def _usd(value: float) -> str:
    return "UNLIMITED (disabled)" if math.isinf(value) else f"${value:,.2f}"


def drift_text(d: CapDrift) -> str:
    return (f"the agent's {d.leg.replace('_', '-')} cap is {_usd(d.gate)} but the signer's "
            f"hard cap is ${d.signer:,.2f} — a trade between the two clears the gate and is "
            f"refused by the signer (logged in shadow; REFUSED after a cut-over to remote)")


def drift_remedy(d: CapDrift) -> str:
    if math.isinf(d.gate):
        return (f"the agent's {_PREF_VERB[d.leg]} cap is disabled while the signer's is "
                f"${d.signer:,.2f}: give the agent one — `polyrob wallet set-cap "
                f"{_PREF_VERB[d.leg]} {d.signer:g}` — or raise `{_KEY[d.leg]}` under "
                f"`[caps]` in {SIGNER_TOML} and `sudo systemctl restart polyrob-signer`")
    return (f"either raise the signer: as root set `{_KEY[d.leg]} = {d.gate:g}` under `[caps]` "
            f"in {SIGNER_TOML}, then `sudo systemctl restart polyrob-signer`; or lower the "
            f"agent: `polyrob wallet set-cap {_PREF_VERB[d.leg]} {d.signer:g}`")
