"""Display-only balance probes for the ledger (spec §5.2).

These feed NO logic. Not every provider exposes a balance, so a balance is
never authoritative and never gates anything — it is rendered when present and
omitted when None. An errored/absent probe returns None ("unknown"), NEVER 0.0
(which would render as an honest-looking "$0.00" lie — the H14b rule).

Both probes are network reads, which is why build_ledger only calls them under
include_balances=True (see unified_ledger §4.1).
"""
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 4.0


async def _get_json(url: str, headers: Optional[Dict[str, str]] = None,
                    timeout: Optional[float] = None) -> Dict[str, Any]:
    """Seam kept for test monkeypatching."""
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=headers or {}, timeout=timeout or _TIMEOUT_SEC) as r:
            r.raise_for_status()
            return await r.json()


async def provider_balance_usd() -> Optional[float]:
    """Remaining provider credit in USD, or None when unknown/unsupported.

    Only OpenRouter exposes one today (GET /api/v1/credits). Every other
    provider returns None — that is a supported, expected outcome, not an error.
    """
    # CHAT_PROVIDER pins the actually-active provider (task_agent_lite.py,
    # cli/config_store.py) — it must win over a stale DEFAULT_PROVIDER, or
    # this probe could show a real-but-wrong provider's balance (a lie).
    provider = (os.getenv("CHAT_PROVIDER") or os.getenv("DEFAULT_PROVIDER") or "").strip().lower()
    if provider != "openrouter":
        return None
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return None
    try:
        payload = await _get_json(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {key}"},
            timeout=_TIMEOUT_SEC,
        )
        data = payload.get("data") or {}
        total = float(data.get("total_credits") or 0.0)
        used = float(data.get("total_usage") or 0.0)
        return round(total - used, 6)
    except Exception:
        logger.debug("provider balance probe failed (fail-open -> None)", exc_info=True)
        return None


async def treasury_balance_usd(user_id: str) -> Optional[float]:
    """On-chain USDC held by the agent wallet across EVERY chain it holds on,
    or None = unknown.

    067 P1b: the read itself is the wallet's (``core/wallet/treasury_balance.py``),
    reached through the kernel hook ``core.money.hooks.treasury_balance_usd``;
    no registered probe reads as unknown. The owner check stays HERE, before
    the hook is asked.
    """
    try:
        # CR-M07: the agent wallet is the OPERATOR's; a non-owner tenant's
        # "treasury balance" is unknown, never the operator's holdings.
        from core.money.authority import owner_refusal
        if owner_refusal(user_id):
            return None
        from core.money.hooks import treasury_balance_usd as _treasury_hook
        return await _treasury_hook()
    except Exception:
        logger.debug("treasury balance probe failed (fail-open -> None)", exc_info=True)
        return None
