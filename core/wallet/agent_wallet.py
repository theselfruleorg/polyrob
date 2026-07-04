"""AgentWallet: the agent's single, operator-funded personal wallet (core-tier).

Hub-and-spoke topology: one master seed → a treasury key + domain-separated
per-venue keys. Hyperliquid trades via its own delegated key (the venue's
built-in withdrawal firewall); x402 pays from its own small float.
"""
from __future__ import annotations

import hashlib
from typing import Callable, List, Optional

from core.wallet.config import WalletConfig
from core.wallet.policy import PolicyGate
from core.wallet.signer import LocalEoaSigner

VENUES = frozenset({"treasury", "x402", "polymarket", "hyperliquid"})

_PBKDF2_ITERS = 100_000


class AgentWallet:
    def __init__(self, config: WalletConfig, audit_sink: Optional[List[dict]] = None,
                 on_record: Optional[Callable[[dict], None]] = None):
        self._config = config
        if config.enabled:
            if not config.master_seed or len(config.master_seed) < 32:
                raise ValueError("AGENT_WALLET_MASTER_SEED must be set and >=32 chars when enabled")
        self._seed = config.master_seed or ""
        self._signers: dict[str, LocalEoaSigner] = {}
        self._policy = PolicyGate(
            max_per_tx_usd=config.max_per_tx_usd,
            audit_sink=audit_sink,
            daily_cap_usd=getattr(config, "daily_cap_usd", None),
            per_venue_daily_cap_usd=getattr(config, "per_venue_daily_cap_usd", None),
            on_record=on_record,
        )

    def _derive_key(self, venue: str) -> bytes:
        # Domain-separated label keeps venue keys independent and recoverable.
        label = f"agent-wallet:{venue}".encode("utf-8")
        return hashlib.pbkdf2_hmac("sha256", self._seed.encode("utf-8"), label, _PBKDF2_ITERS, dklen=32)

    def signer_for(self, venue: str) -> LocalEoaSigner:
        if venue not in VENUES:
            raise ValueError(f"unknown venue '{venue}' (expected one of {sorted(VENUES)})")
        if venue not in self._signers:
            self._signers[venue] = LocalEoaSigner(self._derive_key(venue))
        return self._signers[venue]

    def account_for(self, venue: str):
        return self.signer_for(venue).account

    @property
    def address(self) -> str:
        return self.signer_for("treasury").address

    @property
    def policy(self) -> PolicyGate:
        return self._policy
