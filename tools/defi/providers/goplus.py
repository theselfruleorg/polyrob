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


def _nonempty(raw: Any) -> Optional[bool]:
    """True/False for a list/dict/"0"/"1" shape, None when unreadable."""
    if isinstance(raw, dict) and "status" in raw:
        status = _solana_status(raw)
        return None if status is None else status == "1"
    if isinstance(raw, (list, tuple, dict)):
        return bool(raw)
    status = _solana_status(raw)
    return None if status is None else status == "1"


def _fee_numbers(raw: Any):
    """Every numeric value under a key naming a fee rate / basis points."""
    if isinstance(raw, dict):
        for k, v in raw.items():
            key = str(k).lower()
            if isinstance(v, (dict, list)):
                yield from _fee_numbers(v)
            elif "fee_rate" in key or "basis_point" in key or key == "fee":
                try:
                    yield float(v)
                except (TypeError, ValueError):
                    yield None
    elif isinstance(raw, list):
        for v in raw:
            yield from _fee_numbers(v)


def _transfer_fee_active(raw: Any) -> Optional[bool]:
    """A Token-2022 TransferFee config that CHARGES something.

    ⚠️ Shape not verified against a live Token-2022 response (CR-L10, 2026-09-23).
    GoPlus documents ``transfer_fee`` as an object that is empty when the
    extension is absent; its inner keys are read loosely. A non-empty config
    whose fee rates all read 0 is benign; any other non-empty config (including
    one whose rates we cannot parse) is treated as ACTIVE — fail closed.
    """
    if raw is None:
        return None
    if isinstance(raw, dict) and not raw:
        return False
    if isinstance(raw, list) and not raw:
        return False
    if not isinstance(raw, (dict, list)) or (isinstance(raw, dict) and set(raw) == {"status"}):
        return _nonempty(raw)
    rates = list(_fee_numbers(raw))
    if rates and all(r is not None and r == 0 for r in rates):
        return False
    return True


def _default_state_frozen(raw: Any) -> Optional[bool]:
    """Token-2022 DefaultAccountState = Frozen: every new holder account starts
    frozen, so a buy lands in an account that cannot sell.

    ⚠️ Shape not verified live (CR-L10). Read as the SPL ``AccountState`` enum
    (0 uninitialized, 1 initialized, 2 frozen) or its name; any other value is
    UNREADABLE (a missing check), never "not frozen".
    """
    if isinstance(raw, dict):
        raw = raw.get("status", raw.get("state"))
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in ("2", "frozen"):
        return True
    if text in ("0", "1", "initialized", "uninitialized", ""):
        return False if text else None
    return None


#: CR-L10: Token-2022 extensions that are ACTIVE now, not merely upgradable.
#: The ``*_upgradable`` rows above say someone can change the extension later;
#: these say it already bites — a fee skimmed on every transfer, a hook program
#: that can refuse the sell, an account born frozen. field -> (flag, reader).
#: Absent fields are reported as MISSING (a check that did not run).
_SOLANA_ACTIVE = {
    "transfer_fee": ("transfer_fee_active", _transfer_fee_active),
    "transfer_hook": ("transfer_hook_active", _nonempty),
    "default_account_state": ("default_account_frozen", _default_state_frozen),
}

#: ⚠️ Speculative field name (CR-L10): GoPlus's documented field for a Token-2022
#: PermanentDelegate is ``balance_mutable_authority`` (read in `_SOLANA_RISKS`).
#: A payload that names it explicitly is honoured too, but its absence is NOT
#: reported as a missing check — we do not know the API sends it.
_SOLANA_OPTIONAL = {
    "permanent_delegate": ("permanent_delegate", _nonempty),
}


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
    for field, (flag, read) in list(_SOLANA_ACTIVE.items()) + list(_SOLANA_OPTIONAL.items()):
        raw = data.get(field)
        active = read(raw) if raw is not None else None
        if active is None:
            if field in _SOLANA_ACTIVE:
                missing.append(field)
            continue
        checks[field] = "yes" if active else "no"
        if active:
            flags.append(flag)
    if not checks:
        return ScreenVerdict(available=False)
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    from core.wallet.tokens import clean_name, clean_symbol
    return ScreenVerdict(available=True, checks=checks, flags=flags, missing=missing,
                         symbol=clean_symbol(meta.get("symbol")) or None,
                         name=clean_name(meta.get("name")) or None)


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
        if str(data.get(field)).strip() not in {"0", "1"}:
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

def _fetch(url: str, address: str, timeout: float):
    """``(status_code, payload)`` for one screener call, shared by ``screen``
    and ``holders`` (071 R8: ``token_info`` asked the SAME endpoint twice for
    the same token). Only a 200 answer is cached (60 s, ``core.intel.cache``);
    a failure is never cached, so an outage cannot outlive itself."""
    from core.intel.cache import cache_for
    cache = cache_for("screen")
    key = ("goplus", url, address if not str(address).startswith("0x")
           else str(address).lower())
    hit = cache.get_with_age(key)
    if hit is not None:
        return 200, hit[0]
    from tools.defi.providers import _http
    # Pooled client — see the ~6 s per-connection note in _http.
    from urllib.parse import urlencode
    try:
        payload = _http.get_json(url + "?" + urlencode({"contract_addresses": address}),
                                 timeout=timeout)
    except _http.HttpStatusError as exc:
        return exc.code, None
    # The response must identify the requested contract, not a neighbouring or
    # provider-selected token. Base58 identifiers preserve case.
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict):
        wanted = address.lower() if address.startswith("0x") else address
        matched = {key: value for key, value in result.items()
                   if (key.lower() if address.startswith("0x") else key) == wanted}
        payload = {**payload, "result": matched}
    # 071 review: GoPlus answers HTTP 200 with an error code in the body (e.g.
    # 4029 rate-limited). Only a SUCCESS body (code 1) is cached.
    code = payload.get("code") if isinstance(payload, dict) else None
    if isinstance(payload, dict) and (code in (1, "1") or (code is None and payload.get("result"))):
        cache.put(key, payload)
    return 200, payload


def screen(chain: str, address: str, timeout: float = 8.0) -> ScreenVerdict:
    url = api_url(chain)
    if not url:
        return ScreenVerdict(available=False)
    from core.wallet import chains
    row = chains.get(chain)
    parse = parse_solana_screen if (row and row.family == "svm") else parse_screen
    try:
        status, payload = _fetch(url, address, timeout)
        if status != 200:
            return ScreenVerdict(available=False)
        return parse(payload)
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
        status, payload = _fetch(url, address, timeout)
        if status != 200:
            return HolderReport(available=False,
                                reason=f"the screener answered HTTP {status}")
        return parse_holders(payload)
    except Exception as exc:
        logger.debug("goplus: holders failed for %s/%s", chain, address, exc_info=True)
        return HolderReport(available=False,
                            reason=f"the screener could not be reached ({exc.__class__.__name__})")


def health() -> bool:
    return screen("base", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 5.0).available
