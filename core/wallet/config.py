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
    master_seed: Optional[str]
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


def load_wallet_config(env: Optional[Mapping[str, str]] = None) -> WalletConfig:
    env = os.environ if env is None else env
    network = env.get("AGENT_WALLET_NETWORK", "testnet").strip().lower()
    return WalletConfig(
        enabled=_b(env, "AGENT_WALLET_ENABLED", False),
        backend=env.get("AGENT_WALLET_BACKEND", "local_eoa").strip().lower(),
        master_seed=env.get("AGENT_WALLET_MASTER_SEED"),
        network=network if network in ("testnet", "mainnet") else "testnet",
        # Safety default: a catastrophic per-tx ceiling, NOT a budget. Was
        # $1,000,000 (a typo could drain funds); raise it explicitly if needed.
        max_per_tx_usd=float(env.get("AGENT_WALLET_MAX_PER_TX_USD", "1000")),
        x402_client_enabled=_b(env, "X402_CLIENT_ENABLED", False),
        x402_facilitator_url=env.get("X402_CLIENT_FACILITATOR_URL", TESTNET_FACILITATOR_URL),
        # Rolling 24h spend cap; unset = disabled = legacy behavior (per-tx ceiling only).
        daily_cap_usd=_opt_float(env, "WALLET_DAILY_CAP_USD"),
        per_venue_daily_cap_usd=_load_per_venue_caps(env),
        operational_venue=(env.get("AGENT_WALLET_OPERATIONAL_VENUE", "treasury").strip().lower()
                           or "treasury"),
    )
