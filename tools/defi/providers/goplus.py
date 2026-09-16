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

from tools.defi.providers.base import HolderReport, HolderRow, ScreenVerdict

logger = logging.getLogger(__name__)

API = "https://api.gopluslabs.io/api/v1/token_security"
#: Solana is a DIFFERENT endpoint shape — the network is a path SEGMENT, not the
#: trailing chain id. Composing the EVM url with goplus_id="solana" would build
#: `/token_security/solana`, which 404s, and a 404 renders as UNAVAILABLE, so the
#: mistake would have been invisible as a silent loss of screening.
SOLANA_API = "https://api.gopluslabs.io/api/v1/solana/token_security"
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

#: Solana's risk taxonomy, which shares almost nothing with the EVM one above.
#: There is no honeypot flag and no buy/sell tax; the drain vectors are
#: AUTHORITIES — an account that can still be minted, frozen or closed by
#: someone else, metadata that can be rewritten under you, and Token-2022
#: extensions that can be upgraded after you buy. A freeze authority is the
#: closest thing to a honeypot: a frozen account cannot be sold at all.
#:
#: Values arrive either as a bare "0"/"1" or as {"authority": [...],
#: "status": "0"/"1"}, so both shapes are read.
_SOLANA_RISKS = {
    "mintable": "mintable",
    "freezable": "freezable",
    "closable": "closable",
    "non_transferable": "non_transferable",
    "metadata_mutable": "metadata_mutable",
    "balance_mutable_authority": "balance_mutable_authority",
    "default_account_state_upgradable": "default_account_state_upgradable",
    "transfer_fee_upgradable": "transfer_fee_upgradable",
    "transfer_hook_upgradable": "transfer_hook_upgradable",
}


def _solana_status(raw: Any) -> Optional[str]:
    """"0"/"1" from either shape, or None when the field is absent/unreadable."""
    if isinstance(raw, dict):
        raw = raw.get("status")
    if raw is None:
        return None
    text = str(raw).strip()
    return text if text in ("0", "1") else None


def parse_solana_screen(payload: Optional[Dict[str, Any]]) -> ScreenVerdict:
    """Enumerated Solana verdict. Fails CLOSED, like the EVM parser.

    A payload in which NOT ONE known field is readable is UNAVAILABLE rather
    than clean — otherwise a schema change would quietly turn every Solana
    token into "screened, nothing found", which is the worst possible failure
    for a screen: it looks exactly like safety.
    """
    if not payload or payload.get("code") != 1:
        return ScreenVerdict(available=False)
    result = payload.get("result") or {}
    if not result:
        return ScreenVerdict(available=False)
    # Keyed by the mint address. Solana keys are case-SENSITIVE, so unlike the
    # EVM path there is no lowercasing anywhere near this.
    data = next(iter(result.values()), None)
    if not isinstance(data, dict) or not data:
        return ScreenVerdict(available=False)

    checks: Dict[str, str] = {}
    flags = []
    missing = []
    for field, flag in _SOLANA_RISKS.items():
        status = _solana_status(data.get(field))
        if status is None:
            missing.append(field)
            continue
        checks[field] = "yes" if status == "1" else "no"
        if status == "1":
            flags.append(flag)
    if not checks:
        return ScreenVerdict(available=False)
    return ScreenVerdict(available=True, checks=checks, flags=flags, missing=missing)


def api_url(chain: str) -> Optional[str]:
    """The screening endpoint for *chain*, or None when it is not covered."""
    from core.wallet import chains
    row = chains.get(chain)
    if row is None or not row.goplus_id:
        return None
    if row.family == "svm":
        return SOLANA_API
    return f"{API}/{row.goplus_id}"


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
    missing = []
    for field, flag in _BOOL_RISKS.items():
        if field not in data:
            # Absent = NOT CHECKED. Never recorded as a pass, and now named, so
            # a chain the screener covers only partially cannot render clean.
            missing.append(field)
            continue
        raw = str(data.get(field))
        checks[field] = raw
        if raw == "1":
            flags.append(flag)

    for field in ("buy_tax", "sell_tax"):
        raw = data.get(field)
        # GoPlus sends "" when it did not compute a tax. Recording that as a
        # check that ran reads as "the tax is fine".
        if field not in data or str(raw).strip() == "":
            missing.append(field)
            continue
        checks[field] = str(raw)
        value = _tax(raw)
        if value is not None and value > _TAX_FLAG_THRESHOLD:
            flags.append(f"{field}_{value:.0%}")

    if "is_open_source" in data:
        checks["is_open_source"] = str(data.get("is_open_source"))
        if str(data.get("is_open_source")) == "0":
            flags.append("closed_source")
    else:
        missing.append("is_open_source")

    return ScreenVerdict(available=True, checks=checks, flags=flags, missing=missing)


def _num(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _int(raw: Any) -> Optional[int]:
    value = _num(raw)
    return None if value is None else int(value)


def _flag(raw: Any) -> Optional[bool]:
    """"1"/"0" -> True/False; anything else -> None (the screener did not say)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text in ("1", "true", "True"):
        return True
    if text in ("0", "false", "False"):
        return False
    return None


def _holder_rows(raw: Any) -> list:
    rows = []
    for item in (raw or []):
        if not isinstance(item, dict):
            continue
        address = str(item.get("address") or "").strip()
        if not address:
            continue
        rows.append(HolderRow(
            address=address,
            percent=_num(item.get("percent")),
            is_contract=_flag(item.get("is_contract")),
            is_locked=_flag(item.get("is_locked")),
            tag=(str(item.get("tag")).strip() or None) if item.get("tag") else None,
        ))
    return rows


def parse_holders(payload: Optional[Dict[str, Any]]) -> HolderReport:
    """Pure. The concentration half of the SAME GoPlus answer the screen reads.

    Fails CLOSED with a stated reason. An `available=True` report with an empty
    holder list would read as "nobody holds this token", which is never true and
    is exactly the shape of a silent data loss — so a payload that carries no
    holder rows AND no holder count is UNAVAILABLE, not empty.
    """
    if not payload or payload.get("code") != 1:
        return HolderReport(available=False, reason="the screener returned no result")
    result = payload.get("result") or {}
    data = next(iter(result.values()), None)
    if not isinstance(data, dict) or not data:
        return HolderReport(available=False, reason="the screener returned no result")

    top = _holder_rows(data.get("holders"))
    lp = _holder_rows(data.get("lp_holders"))
    holder_count = _int(data.get("holder_count"))
    if not top and holder_count is None:
        return HolderReport(
            available=False,
            reason=("this chain's screener answered, but sent no holder data — "
                    "holder count, top holders and LP holders were NOT reported"),
            creator_address=(str(data.get("creator_address")).strip() or None
                             if data.get("creator_address") else None),
            creator_percent=_num(data.get("creator_percent")),
            honeypot_with_same_creator=_flag(data.get("honeypot_with_same_creator")),
        )

    return HolderReport(
        available=True,
        holder_count=holder_count,
        total_supply=_num(data.get("total_supply")),
        top_holders=top,
        lp_holders=lp,
        lp_holder_count=_int(data.get("lp_holder_count")),
        lp_total_supply=_num(data.get("lp_total_supply")),
        creator_address=(str(data.get("creator_address")).strip() or None
                         if data.get("creator_address") else None),
        creator_percent=_num(data.get("creator_percent")),
        owner_address=(str(data.get("owner_address")).strip() or None
                       if data.get("owner_address") else None),
        owner_percent=_num(data.get("owner_percent")),
        honeypot_with_same_creator=_flag(data.get("honeypot_with_same_creator")),
    )


# --------------------------------------------------------------------------
# Network boundary — never exercised by unit tests.
# --------------------------------------------------------------------------

def screen(chain: str, address: str, timeout: float = 8.0) -> ScreenVerdict:
    url = api_url(chain)
    if not url:
        return ScreenVerdict(available=False)
    from core.wallet import chains
    row = chains.get(chain)
    parse = parse_solana_screen if (row and row.family == "svm") else parse_screen
    try:
        import httpx
        r = httpx.get(url, params={"contract_addresses": address},
                      timeout=timeout, headers={"user-agent": "polyrob-defi/1.0"})
        if r.status_code != 200:
            return ScreenVerdict(available=False)
        return parse(r.json())
    except Exception:
        logger.debug("goplus: screen failed for %s/%s", chain, address, exc_info=True)
        return ScreenVerdict(available=False)


def holders(chain: str, address: str, timeout: float = 8.0) -> HolderReport:
    """The holder half of the same endpoint `screen` calls.

    Solana is deliberately NOT served here: its endpoint carries an authority
    taxonomy, not holder rows, so pretending otherwise would render an
    available-but-empty report. It returns UNAVAILABLE with that reason.
    """
    url = api_url(chain)
    if not url:
        return HolderReport(available=False,
                            reason=f"GoPlus does not cover chain {chain!r}")
    from core.wallet import chains
    row = chains.get(chain)
    if row is not None and row.family == "svm":
        return HolderReport(available=False, reason=(
            "the Solana screener reports mint/freeze AUTHORITIES, not holder "
            "rows — holder concentration is not available from this source"))
    try:
        import httpx
        r = httpx.get(url, params={"contract_addresses": address},
                      timeout=timeout, headers={"user-agent": "polyrob-defi/1.0"})
        if r.status_code != 200:
            return HolderReport(available=False,
                                reason=f"the screener answered HTTP {r.status_code}")
        return parse_holders(r.json())
    except Exception as exc:
        logger.debug("goplus: holders failed for %s/%s", chain, address, exc_info=True)
        return HolderReport(available=False,
                            reason=f"the screener could not be reached ({exc.__class__.__name__})")


def health() -> bool:
    return screen("base", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 5.0).available
