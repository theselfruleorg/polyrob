"""Display-only balance probes for the ledger (spec §5.2).

These feed NO logic. Not every provider exposes a balance, so a balance is
never authoritative and never gates anything — it is rendered when present and
omitted when None. An errored/absent probe returns None ("unknown"), NEVER 0.0
(which would render as an honest-looking "$0.00" lie — the H14b rule).

Both probes are network reads, which is why build_ledger only calls them under
include_balances=True (see unified_ledger §4.1).
"""
import asyncio
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
    """On-chain USDC balance for the agent wallet, or None when unknown.

    Reuses core/wallet/onchain.py::balances — do NOT write a second reader.
    NOTE: balances() returns a TUPLE (native, usdc), and (None, None) on any
    failure — it is not a dict.
    """
    try:
        from core.wallet.factory import get_agent_wallet
        from core.wallet.onchain import balances as onchain_balances
        wallet = get_agent_wallet()
        addr = getattr(wallet, "address", None) if wallet is not None else None
        if not addr:
            return None
        # onchain_balances() does a BLOCKING urllib.request.urlopen (two RPC
        # calls x 4s timeout each) — run it off the event loop so this async
        # probe can't stall the loop for up to ~8s (this function is called
        # from a live loop once include_balances=True is wired in).
        _native, usdc = await asyncio.to_thread(
            onchain_balances, addr, os.getenv("X402_DEFAULT_CHAIN", "base"))
        if usdc is None:
            return None
        return round(float(usdc), 6)
    except Exception:
        logger.debug("treasury balance probe failed (fail-open -> None)", exc_info=True)
        return None
