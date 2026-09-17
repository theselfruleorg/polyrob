"""Process-level AgentWallet singleton (the agent's wallet is single/operator-owned)."""
from __future__ import annotations

import logging
from typing import Optional

from core.wallet.agent_wallet import AgentWallet
from core.wallet.config import load_wallet_config
from core.wallet.policy import PolicyGate

logger = logging.getLogger(__name__)

_cached: Optional[AgentWallet] = None
_resolved = False
_standalone_policy: Optional[PolicyGate] = None


def _emit_spend_to_event_log(entry: dict) -> None:
    """PolicyGate.on_record hook: mirror each recorded spend into the durable
    telemetry event log so money movement survives a restart and is queryable
    cross-session (telemetry audit 2026-07-04). LAZY import keeps the core/wallet
    tier free of a top-level agents-tier dependency; fail-open throughout."""
    try:
        from core.event_log import get_event_log, event_log_enabled
        if not event_log_enabled():
            return
        # 033 T0.1: PolicyGate is a shared singleton with no execution context, so
        # every wallet_spend row shipped tenantless and the unified ledger matched
        # none of them. Fall back to the ambient identity multi_act binds.
        from core.exec_identity import current_exec_identity
        _uid, _sid = current_exec_identity()
        get_event_log().record(
            "wallet_spend",
            user_id=_uid,
            session_id=_sid,
            source="wallet",
            venue=entry.get("venue"),
            action=entry.get("action"),
            amount_usd=entry.get("amount_usd"),
            counterparty=entry.get("counterparty"),
            result_ref=entry.get("result_ref"),
            ts=entry.get("ts"),
            chain=entry.get("chain"),
        )
    except Exception:
        pass


def _durable_audit_sink():
    """Persist the audit trail so rolling-spend/lifetime tracking survives a
    restart (mainnet prerequisite). Never downgrade to untracked spending."""
    from core.wallet.audit_sink import default_audit_sink
    return default_audit_sink()


def _wallet_without_a_seed(cfg, sink) -> AgentWallet:
    """The wallet a process that holds NO master seed can have.

    Prod moves ``AGENT_WALLET_MASTER_SEED`` into ``/etc/polyrob/wallet.env``,
    loaded ONLY by the agent unit — so ``polyrob-webview.service`` and
    ``polyrob-email.service`` run with the wallet ENABLED and no seed. Absence
    of the seed IS the signal; there is no flag for this, deliberately.

    Fallback order:
      1. the published public identity -> a read-only wallet that knows every
         address and refuses to sign;
      2. nothing published -> the ORIGINAL loud ValueError, unchanged, which
         the CLI turns into "run polyrob wallet init". A genuinely
         unconfigured deploy must not be quietly downgraded to a half-wallet.
    """
    record = None
    try:
        from core.wallet import public_identity
        record = public_identity.read_public_identity()
    except Exception:
        logger.debug("agent wallet: public identity unreadable", exc_info=True)
        record = None
    if not record:
        # Raises "AGENT_WALLET_MASTER_SEED must be set and >=32 chars...".
        return AgentWallet(cfg, audit_sink=sink, on_record=_emit_spend_to_event_log)

    from core.wallet.agent_wallet import PublicOnlyWallet
    wallet = PublicOnlyWallet(cfg, record, audit_sink=sink,
                              on_record=_emit_spend_to_event_log)
    address = ""
    try:
        address = wallet.address
    except Exception:
        address = "(unrecorded)"
    logger.warning(
        "agent wallet: PUBLIC-ONLY mode — this process holds no "
        "AGENT_WALLET_MASTER_SEED, so it can READ the wallet (%s, scheme=%s) "
        "but every signing attempt will be refused. Signing happens in the "
        "agent unit. If this process is meant to move funds, it is missing "
        "the seed env file.", address, wallet.scheme or "unknown")
    return wallet


def get_agent_wallet() -> Optional[AgentWallet]:
    global _cached, _resolved
    if _resolved:
        return _cached
    cfg = load_wallet_config()
    if cfg.enabled:
        sink = _durable_audit_sink()
        if not cfg.master_seed or len(cfg.master_seed) < 32:
            # Branch on the seed BEFORE constructing: AgentWallet raises
            # ValueError for other reasons too (a corrupt derivation meta, a
            # bad AGENT_WALLET_DERIVATION), and swallowing one of those into
            # public-only mode would hide a money-critical misconfiguration.
            _cached = _wallet_without_a_seed(cfg, sink)
        else:
            _cached = AgentWallet(cfg, audit_sink=sink,
                                  on_record=_emit_spend_to_event_log)
            # The seeded process is the only one that CAN publish the public
            # half, so it does, every start. Fail-open — a JSON file that will
            # not write must never stop the agent's wallet.
            try:
                from core.wallet import public_identity
                public_identity.maybe_publish(_cached)
            except Exception:
                logger.debug("agent wallet: identity publish failed (fail-open)",
                             exc_info=True)
    else:
        _cached = None
    _resolved = True
    return _cached


def get_policy_gate() -> PolicyGate:
    """The PolicyGate that guards value-moving actions (trades + payments).

    Returns the agent wallet's gate when the wallet is enabled; otherwise a
    standalone gate built from wallet config so the catastrophic per-tx ceiling
    and daily/venue caps STILL apply to DB-credential trading (which has no
    agent wallet). Cached so rolling-spend/replay state persists within a process.
    """
    wallet = get_agent_wallet()
    if wallet is not None:
        return wallet.policy
    global _standalone_policy
    if _standalone_policy is None:
        cfg = load_wallet_config()
        # M3 (2026-07-15): the standalone gate guards DB-credential trading, which
        # has no agent wallet — but it still needs the SAME durable audit sink the
        # wallet path uses, or its rolling-24h caps + replay guard reset every
        # restart (a mainnet prerequisite; see audit_sink module docstring).
        # Fail-open: fall back to in-memory if the durable sink can't be created.
        sink = _durable_audit_sink()
        _standalone_policy = PolicyGate(
            max_per_tx_usd=cfg.max_per_tx_usd,
            audit_sink=sink,
            daily_cap_usd=cfg.daily_cap_usd,
            per_venue_daily_cap_usd=cfg.per_venue_daily_cap_usd,
            on_record=_emit_spend_to_event_log,
            cap_resolver=getattr(cfg, "cap_resolver", None),
        )
    return _standalone_policy


def reset_agent_wallet_cache() -> None:
    global _cached, _resolved, _standalone_policy
    _cached = None
    _resolved = False
    _standalone_policy = None
