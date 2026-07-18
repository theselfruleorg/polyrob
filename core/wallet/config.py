"""Agent-wallet configuration (env-driven, default-safe)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

TESTNET_FACILITATOR_URL = "https://x402.org/facilitator"

_TRUE = {"1", "true", "yes", "on"}


def _b(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    return default if raw is None else raw.strip().lower() in _TRUE


@dataclass(frozen=True)
class WalletConfig:
    enabled: bool
    backend: str
    # L1 (2026-07-15): repr=False so the auto-generated __repr__ never embeds the
    # raw seed (still a required field — no default; kept before the defaulted
    # fields below). One logger.debug(f"...{cfg}") away from a log leak otherwise.
    master_seed: Optional[str] = field(repr=False)
    network: str
    max_per_tx_usd: float
    x402_client_enabled: bool
    x402_facilitator_url: str
    daily_cap_usd: Optional[float] = None
    per_venue_daily_cap_usd: Dict[str, float] = field(default_factory=dict)
    # The venue key that same-chain spend paths (x402, generic payments) SIGN with.
    # Default "treasury" so the address the owner funds (== AgentWallet.address) is the
    # address actually spent from. "Venue" elsewhere (policy caps) stays an accounting
    # label. Hyperliquid keeps its own delegated key regardless of this.
    operational_venue: str = "treasury"


def _opt_float(env: Mapping[str, str], key: str) -> Optional[float]:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _load_per_venue_caps(env: Mapping[str, str]) -> Dict[str, float]:
    """Parse WALLET_VENUE_DAILY_CAP_<VENUE>_USD env vars into {venue: cap}."""
    prefix, suffix = "WALLET_VENUE_DAILY_CAP_", "_USD"
    caps: Dict[str, float] = {}
    for key in env:
        if key.startswith(prefix) and key.endswith(suffix):
            venue = key[len(prefix):-len(suffix)].lower()
            val = _opt_float(env, key)
            if venue and val is not None:
                caps[venue] = val
    return caps


def effective_daily_cap_usd(user_id: Optional[str], home_dir,
                            env: Optional[Mapping[str, str]] = None) -> Optional[float]:
    """Owner's rolling-24h wallet spend cap: pref (min-merged, spec
    ``budget.wallet_daily_usd``) over ``WALLET_DAILY_CAP_USD``.

    ``WALLET_DAILY_CAP_USD`` unset means "no cap" (legacy) — that is NOT a
    ceiling of 0, so it is passed as ``env_value=None`` (not folded through a
    default) so a pref ALONE can still set a cap when the operator set none.
    No pref file present => byte-identical to ``load_wallet_config(env).daily_cap_usd``
    (owner-UX P1 T4). Wired into ``load_wallet_config`` -> ``PolicyGate`` (G-13)."""
    from core import prefs
    env_value = _opt_float(os.environ if env is None else env, "WALLET_DAILY_CAP_USD")
    return prefs.resolve("budget.wallet_daily_usd", user_id, home_dir,
                         env_value=env_value, default=None)


def effective_max_per_tx_usd(user_id: Optional[str], home_dir,
                             env: Optional[Mapping[str, str]] = None) -> float:
    """Owner's per-transaction wallet ceiling: pref (min-merged, spec
    ``budget.wallet_per_tx_usd``) over ``AGENT_WALLET_MAX_PER_TX_USD``.

    Unlike :func:`effective_daily_cap_usd`, the env leg here is ALWAYS a
    concrete value (the $1000 catastrophic-loss safety default when unset —
    not a "no cap" sentinel), so this is a pure min-merge: a pref can only
    lower it further, never widen it via a None-passthrough. No pref file
    present => byte-identical to ``load_wallet_config(env).max_per_tx_usd``
    (owner-UX G-13)."""
    from core import prefs
    env_value = float((os.environ if env is None else env).get(
        "AGENT_WALLET_MAX_PER_TX_USD", "1000"))
    return prefs.resolve("budget.wallet_per_tx_usd", user_id, home_dir,
                         env_value=env_value, default=env_value)


def _fail_open_owner_user_id() -> Optional[str]:
    """Representative tenant for the process-level wallet singleton's pref
    lookup (the agent wallet is single/operator-owned — see
    ``core/wallet/factory.py``). Fail-open to ``None`` => no pref match =>
    legacy env-only value, mirroring ``agents.task.goals.dispatcher._tick_owner_user_id``."""
    try:
        from core.instance import resolve_owner_principal
        return resolve_owner_principal()
    except Exception:
        return None


def _fail_open_home_dir():
    """Pref-storage home for the process-level wallet singleton. Fail-open to
    ``None`` => legacy env-only value (any downstream error is ALSO caught by
    the caller's own try/except, so this can never crash config loading)."""
    try:
        from core.paths import polyrob_home
        return polyrob_home()
    except Exception:
        return None


def load_wallet_config(env: Optional[Mapping[str, str]] = None, *,
                       user_id: Optional[str] = None,
                       home_dir: Optional[object] = None) -> WalletConfig:
    """Build the wallet config PolicyGate is built from.

    ``daily_cap_usd``/``max_per_tx_usd`` are resolved through
    :func:`effective_daily_cap_usd`/:func:`effective_max_per_tx_usd` (G-13,
    owner-UX): a per-tenant preference can only TIGHTEN these two
    env-authoritative caps, never raise or disable them. ``user_id``/``home_dir``
    default to a fail-open owner/home resolution (this is a process-level,
    single-owner singleton — see ``core/wallet/factory.py``); ANY failure in
    that resolution, or in the prefs module itself, leaves the plain env value
    completely unchanged (fail-open — prefs are advisory, never a crash risk
    for money config).
    """
    env = os.environ if env is None else env
    network = env.get("AGENT_WALLET_NETWORK", "testnet").strip().lower()
    # Safety default: a catastrophic per-tx ceiling, NOT a budget. Was
    # $1,000,000 (a typo could drain funds); raise it explicitly if needed.
    max_per_tx_usd = float(env.get("AGENT_WALLET_MAX_PER_TX_USD", "1000"))
    # Rolling 24h spend cap; unset = disabled = legacy behavior (per-tx ceiling only).
    daily_cap_usd = _opt_float(env, "WALLET_DAILY_CAP_USD")
    resolved_user = user_id if user_id is not None else _fail_open_owner_user_id()
    resolved_home = home_dir if home_dir is not None else _fail_open_home_dir()
    try:
        daily_cap_usd = effective_daily_cap_usd(resolved_user, resolved_home, env=env)
    except Exception:
        pass  # fail-open: prefs unavailable/raising -> env value unchanged
    try:
        max_per_tx_usd = effective_max_per_tx_usd(resolved_user, resolved_home, env=env)
    except Exception:
        pass  # fail-open: prefs unavailable/raising -> env value unchanged
    return WalletConfig(
        enabled=_b(env, "AGENT_WALLET_ENABLED", False),
        backend=env.get("AGENT_WALLET_BACKEND", "local_eoa").strip().lower(),
        master_seed=env.get("AGENT_WALLET_MASTER_SEED"),
        network=network if network in ("testnet", "mainnet") else "testnet",
        max_per_tx_usd=max_per_tx_usd,
        x402_client_enabled=_b(env, "X402_CLIENT_ENABLED", False),
        x402_facilitator_url=env.get("X402_CLIENT_FACILITATOR_URL", TESTNET_FACILITATOR_URL),
        daily_cap_usd=daily_cap_usd,
        per_venue_daily_cap_usd=_load_per_venue_caps(env),
        operational_venue=(env.get("AGENT_WALLET_OPERATIONAL_VENUE", "treasury").strip().lower()
                           or "treasury"),
    )
