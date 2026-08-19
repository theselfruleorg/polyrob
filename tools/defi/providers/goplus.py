"""GoPlus Security — token safety screening (keyless).

Fails CLOSED: an unreachable or silent screener yields ``available=False``,
never a pass. The verdict enumerates the checks that actually ran rather than
returning a boolean, because "passed" reads as "safe" and a check that did not
run is not a check that passed.

Screening is a heuristic over known patterns — a novel rug passes it. It is
defence in depth, not the bound.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from tools.defi.providers.base import ScreenVerdict

logger = logging.getLogger(__name__)

API = "https://api.gopluslabs.io/api/v1/token_security"
def _chain_ids():
    """GoPlus chain ids, from the registry. A chain GoPlus does not cover has
    no id and `screen` reports UNAVAILABLE rather than an unscreened pass."""
    from core.wallet import chains
    return {r.name: r.goplus_id for r in chains.all_rows() if r.goplus_id}


CHAIN_IDS = _chain_ids()

name = "goplus"

#: field -> the flag raised when it is "1". Kept explicit so a schema change
#: shows up as a missing check rather than a silent pass.
_BOOL_RISKS = {
    "is_honeypot": "honeypot",
    "cannot_sell_all": "cannot_sell_all",
    "cannot_buy": "cannot_buy",
    "is_blacklisted": "blacklist",
    "is_proxy": "proxy_upgradeable",
    "is_mintable": "mintable",
    "transfer_pausable": "transfer_pausable",
    "hidden_owner": "hidden_owner",
    "can_take_back_ownership": "ownership_reclaimable",
    "selfdestruct": "selfdestruct",
    "slippage_modifiable": "slippage_modifiable",
    "personal_slippage_modifiable": "personal_slippage_modifiable",
    "trading_cooldown": "trading_cooldown",
}

#: Above this, a tax is treated as a risk flag rather than a fee.
_TAX_FLAG_THRESHOLD = 0.10


def _tax(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_screen(payload: Optional[Dict[str, Any]]) -> ScreenVerdict:
    """Enumerated verdict. Anything other than a populated, code==1 result is
    UNAVAILABLE — no data is not a clean bill of health."""
    if not payload or payload.get("code") != 1:
        return ScreenVerdict(available=False)
    result = payload.get("result") or {}
    if not result:
        return ScreenVerdict(available=False)
    # GoPlus keys the result by lowercased address; this call is one address.
    data = next(iter(result.values()), None)
    if not isinstance(data, dict) or not data:
        return ScreenVerdict(available=False)

    checks: Dict[str, str] = {}
    flags = []
    for field, flag in _BOOL_RISKS.items():
        if field not in data:
            continue           # absent = not checked; deliberately not recorded as a pass
        raw = str(data.get(field))
        checks[field] = raw
        if raw == "1":
            flags.append(flag)

    for field in ("buy_tax", "sell_tax"):
        if field not in data:
            continue
        raw = data.get(field)
        checks[field] = str(raw)
        value = _tax(raw)
        if value is not None and value > _TAX_FLAG_THRESHOLD:
            flags.append(f"{field}_{value:.0%}")

    if "is_open_source" in data:
        checks["is_open_source"] = str(data.get("is_open_source"))
        if str(data.get("is_open_source")) == "0":
            flags.append("closed_source")

    return ScreenVerdict(available=True, checks=checks, flags=flags)


# --------------------------------------------------------------------------
# Network boundary — never exercised by unit tests.
# --------------------------------------------------------------------------

def screen(chain: str, address: str, timeout: float = 8.0) -> ScreenVerdict:
    chain_id = CHAIN_IDS.get(chain)
    if not chain_id:
        return ScreenVerdict(available=False)
    try:
        import httpx
        r = httpx.get(f"{API}/{chain_id}", params={"contract_addresses": address},
                      timeout=timeout, headers={"user-agent": "polyrob-defi/1.0"})
        if r.status_code != 200:
            return ScreenVerdict(available=False)
        return parse_screen(r.json())
    except Exception:
        logger.debug("goplus: screen failed for %s/%s", chain, address, exc_info=True)
        return ScreenVerdict(available=False)


def health() -> bool:
    return screen("base", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 5.0).available
