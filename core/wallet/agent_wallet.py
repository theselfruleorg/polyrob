"""AgentWallet: the agent's single, operator-funded personal wallet (core-tier).

Hub-and-spoke topology: one master seed → a treasury key + domain-separated
per-venue keys. Hyperliquid trades via its own delegated key (the venue's
built-in withdrawal firewall). Same-chain spend paths (x402, generic payments)
sign with the OPERATIONAL venue (default 'treasury') so the address the owner
funds (`AgentWallet.address`) is exactly the address spent from.
"""
from __future__ import annotations

from typing import Any, Callable, List, Mapping, Optional

from core.wallet.config import WalletConfig
from core.wallet.policy import PolicyGate
from core.wallet.signer import LocalEoaSigner

VENUES = frozenset({"treasury", "x402", "polymarket", "hyperliquid"})
# Venues whose derived key holds a same-chain float the agent spends directly.
# hyperliquid/polymarket are delegated/managed elsewhere (their derived key never
# holds funds), so the operational venue is clamped to these to avoid making a
# generic payment sign with — and surface for funding — a non-fundable key.
_SPEND_VENUES = frozenset({"treasury", "x402"})


class WalletSigningUnavailable(RuntimeError):
    """This process knows the wallet's addresses but holds no key.

    Prod loads ``AGENT_WALLET_MASTER_SEED`` from ``/etc/polyrob/wallet.env``,
    which only the agent unit reads; the console and the email surface run with
    the wallet ENABLED and no seed so they can show the book without being able
    to move it. Every read still answers — an address is public — and every
    attempt to SIGN raises this, loudly and before anything is broadcast.
    """


class PublicOnlySigner:
    """One address, no authority — the seedless twin of ``LocalEoaSigner``.

    It exists because an address is read THROUGH a signer almost everywhere in
    this tree (``wallet.operational_signer().address``,
    ``wallet.signer_for("treasury").address``). Refusing to hand back an object
    at all would break every one of those READS to stop a write that this
    object already refuses.

    Serves both families: an EVM venue and the Solana account differ here only
    in what the refusal says, because neither can do anything.
    """

    def __init__(self, address: str, *, venue: str = "", family: str = "evm"):
        self._address = str(address)
        self._venue = venue
        self._family = family

    @property
    def address(self) -> str:
        return self._address

    @property
    def account(self):
        """The raw account IS signing authority — never fabricated."""
        raise WalletSigningUnavailable(self._refusal("hand out the raw account"))

    def _refusal(self, what: str) -> str:
        who = f" for {self._family}:{self._venue}" if self._venue else ""
        return (
            f"cannot {what}{who} — this process holds no seed; signing happens "
            f"in the agent unit (AGENT_WALLET_MASTER_SEED is loaded only by "
            f"polyrob.service, from /etc/polyrob/wallet.env). The address "
            f"{self._address} is public and correct; the key is not here.")

    def sign_message(self, data):
        raise WalletSigningUnavailable(self._refusal("sign a message"))

    def sign_typed_data(self, domain, types, message):
        raise WalletSigningUnavailable(self._refusal("sign typed data"))

    def sign_transaction(self, tx):
        raise WalletSigningUnavailable(self._refusal("sign a transaction"))

    def sign_transaction_with(self, tx, extra_keypairs):
        raise WalletSigningUnavailable(self._refusal("sign a transaction"))

    def __repr__(self) -> str:
        return f"<PublicOnlySigner address={self._address} (no key)>"


class AgentWallet:
    #: Flipped off by :class:`PublicOnlyWallet`, the subclass a process with no
    #: seed gets. Read it (via the ``signing_available`` property) when a
    #: surface wants to SAY which half it is talking to; never to decide
    #: whether to try signing — attempting it and being refused is the honest
    #: path, and the refusal names where the key lives.
    _signing_available = True

    def __init__(self, config: WalletConfig, audit_sink: Optional[List[dict]] = None,
                 on_record: Optional[Callable[[dict], None]] = None):
        self._config = config
        if config.enabled and self._signing_available:
            if not config.master_seed or len(config.master_seed) < 32:
                raise ValueError("AGENT_WALLET_MASTER_SEED must be set and >=32 chars when enabled")
        self._seed = config.master_seed or ""
        self._scheme = self._resolve_scheme()
        # H1 (2026-07-15): warn loudly if a mnemonic seed is being derived as legacy
        # with no recorded scheme (the silent address-flip footgun). Fail-open — a
        # warning must never block wallet construction.
        try:
            from core.wallet import derivation as _derivation
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

    def _resolve_scheme(self) -> str:
        """The recorded derivation scheme. A seam, not a convenience: the
        public-only subclass reads it from the identity record instead of from
        ``meta.json``, which a seedless unit may not share."""
        from core.wallet import derivation as _derivation
        return _derivation.resolve_scheme()

    def _derive_key(self, venue: str) -> bytes:
        from core.wallet import derivation as _derivation
        return _derivation.derive_key(self._seed, venue, self._scheme)

    def signer_for(self, venue: str) -> LocalEoaSigner:
        if venue not in VENUES:
            raise ValueError(f"unknown venue '{venue}' (expected one of {sorted(VENUES)})")
        if venue not in self._signers:
            self._signers[venue] = LocalEoaSigner(self._derive_key(venue))
        return self._signers[venue]

    def address_for_venue(self, venue: str) -> str:
        """The PUBLIC address of *venue*.

        The one accessor a caller can use without knowing whether this process
        holds a seed. ``signer_for(venue).address`` still works and is what
        most of the tree says today; this exists so new code does not have to
        reach through a signer to read something public.
        """
        return self.signer_for(venue).address

    def account_for(self, venue: str):
        return self.signer_for(venue).account

    @property
    def scheme(self) -> str:
        """The derivation scheme the addresses came from ('legacy'/'bip44')."""
        return self._scheme

    @property
    def signing_available(self) -> bool:
        """False on a process that holds no master seed."""
        return self._signing_available

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


class PublicOnlyWallet(AgentWallet):
    """The wallet as a process that holds NO master seed can know it.

    Built by ``core.wallet.factory`` from ``public_identity.json`` when the
    wallet is ENABLED and the seed is absent — the shape prod's console and
    email units run in once the seed lives in ``/etc/polyrob/wallet.env``.

    What it keeps: every address, the derivation scheme they came from, the
    operational-venue choice, and the PolicyGate (caps are not authority, and a
    read surface renders them). What it cannot do: derive a key, hand out an
    account, or sign anything — each raises :class:`WalletSigningUnavailable`
    naming the unit that does hold the seed.

    ⚠️ The addresses come from a FILE, not from a derivation. They are only as
    true as the last time the seeded agent published them, which is why the
    writer re-derives before it trusts an existing record
    (``public_identity.is_current``) and why nothing here is allowed to
    silently invent an address for a venue the record does not name.
    """

    _signing_available = False

    def __init__(self, config: WalletConfig, identity: Mapping[str, Any],
                 audit_sink: Optional[List[dict]] = None,
                 on_record: Optional[Callable[[dict], None]] = None):
        self._identity: dict = dict(identity or {})
        self._addresses: dict = {
            str(venue): str(addr)
            for venue, addr in (self._identity.get("evm") or {}).items() if addr}
        super().__init__(config, audit_sink=audit_sink, on_record=on_record)

    # -- identity ----------------------------------------------------------

    def _resolve_scheme(self) -> str:
        # NOT `derivation.resolve_scheme()`: a seedless unit may not share the
        # agent's meta.json, and a wrong answer here would be a claim about
        # addresses this process did not derive. The record is the only source.
        return str(self._identity.get("scheme") or "")

    @property
    def identity(self) -> dict:
        """The record this wallet was built from (public, read-only)."""
        return dict(self._identity)

    @property
    def network(self) -> str:
        # The RECORD wins: it describes the wallet the agent actually runs,
        # which is what the addresses belong to. Config is the fallback.
        return str(self._identity.get("network") or super().network)

    @property
    def operational_venue(self) -> str:
        """Whichever venue the seeded agent recorded as operational.

        Config is only the fallback — a seedless unit's env need not carry
        ``AGENT_WALLET_OPERATIONAL_VENUE``, and guessing would point
        ``.address`` at a key the agent does not spend from.

        ⚠️ The last resort scans the SPEND venues only, never "any recorded
        address". ``address`` is the fund-me address an owner reads; naming a
        delegated hyperliquid/polymarket key there is the exact
        fund-the-wrong-address footgun the base class clamps against. With
        neither spend venue recorded this returns the config value and lets
        ``signer_for`` refuse by name — an honest refusal beats a plausible
        wrong address.
        """
        recorded = str(self._identity.get("operational_venue") or "").strip().lower()
        if recorded in self._addresses:
            return recorded
        from_config = super().operational_venue
        if from_config in self._addresses:
            return from_config
        for venue in sorted(_SPEND_VENUES):
            if venue in self._addresses:
                return venue
        return from_config

    # -- reads (public) ----------------------------------------------------

    def signer_for(self, venue: str) -> PublicOnlySigner:
        if venue not in VENUES:
            raise ValueError(f"unknown venue '{venue}' (expected one of {sorted(VENUES)})")
        address = self._addresses.get(venue)
        if not address:
            raise WalletSigningUnavailable(
                f"this process holds no seed, and the published wallet identity "
                f"records no address for venue '{venue}' — it cannot be derived "
                f"here. Restart the agent unit (which holds "
                f"AGENT_WALLET_MASTER_SEED) to republish it.")
        return PublicOnlySigner(address, venue=venue)

    def solana_signer(self, account: int = 0) -> PublicOnlySigner:
        if int(account) != 0:
            raise WalletSigningUnavailable(
                f"this process holds no seed, so Solana account {int(account)} "
                f"cannot be derived; only the agent's own account (0) is "
                f"published. Derived accounts 1..n are only ever scanned by the "
                f"unit that holds the seed.")
        address = self._identity.get("solana")
        if not address:
            raise WalletSigningUnavailable(
                "this process holds no seed, and the published wallet identity "
                "records no Solana address — it cannot be derived here.")
        return PublicOnlySigner(str(address), venue="account0", family="svm")

    # -- refusals ----------------------------------------------------------

    def _derive_key(self, venue: str) -> bytes:
        raise WalletSigningUnavailable(
            f"cannot derive the '{venue}' key — this process holds no seed; "
            f"signing happens in the agent unit (AGENT_WALLET_MASTER_SEED is "
            f"loaded only by polyrob.service, from /etc/polyrob/wallet.env).")

    def account_for(self, venue: str):
        raise WalletSigningUnavailable(
            f"cannot hand out the raw '{venue}' account — this process holds "
            f"no seed; signing happens in the agent unit. Use "
            f"address_for_venue({venue!r}) if an ADDRESS is what is wanted.")
