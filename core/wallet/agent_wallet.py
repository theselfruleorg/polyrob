"""AgentWallet: the agent's single, operator-funded personal wallet (core-tier).

Hub-and-spoke topology: one master seed → a treasury key + domain-separated
per-venue keys. Hyperliquid trades via its own delegated key (the venue's
built-in withdrawal firewall). Same-chain spend paths (x402, generic payments)
sign with the OPERATIONAL venue (default 'treasury') so the address the owner
funds (`AgentWallet.address`) is exactly the address spent from.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from core.wallet.config import WalletConfig
from core.wallet.policy import PolicyGate
from core.wallet.signer import LocalEoaSigner

VENUES = frozenset({"treasury", "x402", "polymarket", "hyperliquid"})
# Venues whose derived key holds a same-chain float the agent spends directly.
# hyperliquid/polymarket are delegated/managed elsewhere (their derived key never
# holds funds), so the operational venue is clamped to these to avoid making a
# generic payment sign with — and surface for funding — a non-fundable key.
_SPEND_VENUES = frozenset({"treasury", "x402"})


class AgentWallet:
    def __init__(self, config: WalletConfig, audit_sink: Optional[List[dict]] = None,
                 on_record: Optional[Callable[[dict], None]] = None):
        self._config = config
        if config.enabled:
            if not config.master_seed or len(config.master_seed) < 32:
                raise ValueError("AGENT_WALLET_MASTER_SEED must be set and >=32 chars when enabled")
        self._seed = config.master_seed or ""
        from core.wallet import derivation as _derivation
        self._scheme = _derivation.resolve_scheme()
        # H1 (2026-07-15): warn loudly if a mnemonic seed is being derived as legacy
        # with no recorded scheme (the silent address-flip footgun). Fail-open — a
        # warning must never block wallet construction.
        try:
            _derivation.maybe_warn_legacy_mnemonic(self._seed, self._scheme)
        except Exception:
            pass
        self._signers: dict[str, LocalEoaSigner] = {}
        self._policy = PolicyGate(
            max_per_tx_usd=config.max_per_tx_usd,
            audit_sink=audit_sink,
            daily_cap_usd=getattr(config, "daily_cap_usd", None),
            per_venue_daily_cap_usd=getattr(config, "per_venue_daily_cap_usd", None),
            on_record=on_record,
        )

    def _derive_key(self, venue: str) -> bytes:
        from core.wallet import derivation as _derivation
        return _derivation.derive_key(self._seed, venue, self._scheme)

    def signer_for(self, venue: str) -> LocalEoaSigner:
        if venue not in VENUES:
            raise ValueError(f"unknown venue '{venue}' (expected one of {sorted(VENUES)})")
        if venue not in self._signers:
            self._signers[venue] = LocalEoaSigner(self._derive_key(venue))
        return self._signers[venue]

    def account_for(self, venue: str):
        return self.signer_for(venue).account

    @property
    def operational_venue(self) -> str:
        """The venue key same-chain spend paths sign with (default 'treasury')."""
        venue = getattr(self._config, "operational_venue", "treasury") or "treasury"
        # Clamp to same-chain SPEND venues — hyperliquid/polymarket keys never hold a
        # spendable float, so pointing the operational venue at them would strand funds.
        return venue if venue in _SPEND_VENUES else "treasury"

    def operational_signer(self) -> LocalEoaSigner:
        """Signer for the operational venue — the SINGLE source of truth for
        'which key does the agent spend from' on same-chain paths (x402, generic).
        Keeping this and `address` in lockstep is what prevents the fund-the-wrong-
        address footgun (the funded address == the spent address)."""
        return self.signer_for(self.operational_venue)

    @property
    def address(self) -> str:
        # The owner-facing "fund me" address MUST equal the address actually spent
        # from, so it tracks the operational venue (not a hardcoded 'treasury').
        return self.operational_signer().address

    # -- Solana (Phase 2) --------------------------------------------------
    # A SECOND family off the same seed, not a second wallet. The EVM address is
    # untouched by this existing — different curve, different derivation path,
    # different account. The owner must fund both. Value moves through the
    # guarded `defi_trade.solana_swap` verb (Phases 3-4: signer, rail,
    # simulation, x402 SVM settle), gated by SOLANA_TRADE_ENABLED.

    def solana_signer(self, account: int = 0):
        """Signer for the agent's Solana account. Cached, like the EVM ones."""
        key = f"__solana:{int(account)}"
        cached = self._signers.get(key)
        if cached is None:
            from core.wallet.solana_signer import SolanaSigner
            seed = getattr(self._config, "master_seed", None)
            if not seed:
                raise ValueError("no master seed configured")
            cached = SolanaSigner.from_mnemonic(seed, account)
            self._signers[key] = cached
        return cached

    @property
    def solana_address(self) -> str:
        """The agent's Solana address. NOT interchangeable with `address` —
        showing one where the other belongs is how funds get stranded on a chain
        the key cannot spend on."""
        return self.solana_signer().address

    @property
    def config(self) -> WalletConfig:
        return self._config

    @property
    def network(self) -> str:
        return getattr(self._config, "network", "testnet")

    @property
    def policy(self) -> PolicyGate:
        return self._policy
