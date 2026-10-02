"""The treasury balance probe — the wallet side of the kernel hook (067 P1b).

Moved from ``modules/credits/balances.py`` (which now asks
``core.money.hooks.treasury_balance_usd``): the on-chain read is the wallet's
knowledge, not the credits module's. Registered by ``core/wallet/__init__.py``
(067 P5a) until the wallet becomes a pack (P5b).
"""
import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


async def treasury_balance_usd() -> Optional[float]:
    """On-chain USDC held by the agent wallet across EVERY chain it holds on.

    The caller has already done the owner check (CR-M07): the agent wallet is
    the OPERATOR's, so a non-owner tenant never reaches this probe.

    Reuses core/wallet/onchain.py::balances and solana_onchain.token_balances —
    do NOT write a third reader. NOTE: balances() returns a TUPLE
    (native, usdc), and (None, None) on any failure — it is not a dict.

    The Solana leg is not optional garnish. The wallet has a Solana address with
    its own USDC, and reading only X402_DEFAULT_CHAIN reported a PARTIAL figure
    as the treasury total — a silent undercount, which is the same lie as
    rendering unknown as $0.00 (the H14b rule). So: a wallet with no Solana
    identity has nothing omitted and the EVM leg IS the complete answer, but
    once a Solana address exists, a failed read there makes the TOTAL unknown
    rather than quietly shrinking it to the EVM leg.
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
        total = float(usdc)

        sol_addr = getattr(wallet, "solana_address", None)
        if sol_addr:
            sol_usdc = await asyncio.to_thread(_solana_usdc_units, sol_addr)
            if sol_usdc is None:
                return None  # partial total is worse than an honest unknown
            total += sol_usdc
        return round(total, 6)
    except Exception:
        logger.debug("treasury balance probe failed (fail-open -> None)", exc_info=True)
        return None


def _solana_usdc_units(owner: str) -> Optional[float]:
    """USDC held at *owner* on Solana, or None when the read failed.

    Blocking on purpose — the caller dispatches it with asyncio.to_thread, the
    same way it treats the EVM probe.
    """
    from core.wallet import chains, solana_onchain
    row = chains.get("solana")
    mint = row.usdc if row else None
    if not mint:
        return None
    raw = solana_onchain.token_balances(owner)
    if raw is None:
        return None  # a failed enumeration is UNKNOWN, never an empty wallet
    return raw.get(mint, 0) / 1_000_000  # USDC is 6 decimals on Solana too
