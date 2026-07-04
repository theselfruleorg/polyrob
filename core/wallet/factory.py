"""Process-level AgentWallet singleton (the agent's wallet is single/operator-owned)."""
from __future__ import annotations

from typing import Optional

from core.wallet.agent_wallet import AgentWallet
from core.wallet.config import load_wallet_config
from core.wallet.policy import PolicyGate

_cached: Optional[AgentWallet] = None
_resolved = False
_standalone_policy: Optional[PolicyGate] = None


def _emit_spend_to_event_log(entry: dict) -> None:
    """PolicyGate.on_record hook: mirror each recorded spend into the durable
    telemetry event log so money movement survives a restart and is queryable
    cross-session (telemetry audit 2026-07-04). LAZY import keeps the core/wallet
    tier free of a top-level agents-tier dependency; fail-open throughout."""
    try:
        from agents.task.telemetry.event_log import get_event_log, event_log_enabled
        if not event_log_enabled():
            return
        get_event_log().record(
            "wallet_spend",
            source="wallet",
            venue=entry.get("venue"),
            action=entry.get("action"),
            amount_usd=entry.get("amount_usd"),
            counterparty=entry.get("counterparty"),
            result_ref=entry.get("result_ref"),
            ts=entry.get("ts"),
        )
    except Exception:
        pass


def get_agent_wallet() -> Optional[AgentWallet]:
    global _cached, _resolved
    if _resolved:
        return _cached
    cfg = load_wallet_config()
    if cfg.enabled:
        # Persist the audit trail so rolling-spend/lifetime tracking survives a
        # restart (mainnet prerequisite). Fail-open: fall back to in-memory if the
        # durable sink can't be created.
        sink = None
        try:
            from core.wallet.audit_sink import default_audit_sink
            sink = default_audit_sink()
        except Exception:
            sink = None
        _cached = AgentWallet(cfg, audit_sink=sink, on_record=_emit_spend_to_event_log)
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
        _standalone_policy = PolicyGate(
            max_per_tx_usd=cfg.max_per_tx_usd,
            daily_cap_usd=cfg.daily_cap_usd,
            per_venue_daily_cap_usd=cfg.per_venue_daily_cap_usd,
            on_record=_emit_spend_to_event_log,
        )
    return _standalone_policy


def reset_agent_wallet_cache() -> None:
    global _cached, _resolved, _standalone_policy
    _cached = None
    _resolved = False
    _standalone_policy = None
