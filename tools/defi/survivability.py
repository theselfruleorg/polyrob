"""Survivability fields on ``token_info`` — the hunt's hard screen (086 §3.6, W9).

The Aug/Sep record: 6 of 18 Base tickets died in their pool within hours. The
contract screen (``token_screen``) asks "can a holder sell and keep the
balance"; this block asks "will the pool still be there". Each gate is ONE
field with a status:

* ``pass`` / ``fail`` — measured against the line in 086 §3.6;
* ``unknown`` — no source answered. NEVER safe: any unknown gate lowers the
  tier ceiling by one step (A→B→C, never below C, never up);
* ``info`` — a fact with no line drawn (``fresh_wallet_ratio``: no keyless
  source exists, so it is reported unknown and does not gate).

Sources — nothing here buys data, and almost everything is reused:

=====================  ===================================================
pool age, depth        GeckoTerminal ``/tokens/{addr}/pools`` (one cached call)
liquidity              the agreed price quote ``token_info`` already read
LP locked / burned     EVM: GoPlus ``lp_holders`` (same answer as holders);
                       Solana: RugCheck ``lpLockedPct`` (already in the screen)
sell simulation        EVM: Honeypot.is / GoPlus ``is_honeypot`` (screen);
                       Solana: none keyless → unknown
sell tax               EVM: ``sell_tax`` (screen); Solana: Token-2022 fee (RPC)
mint / freeze (SVM)    the chain's own mint account (screen, ``rpc``)
owner powers (EVM)     GoPlus ``owner_change_balance`` + ``slippage_modifiable``
top-holder share       the holder report (top 10, contracts/programs excluded)
bundled supply (SVM)   RugCheck full report: insider networks' current holding
=====================  ===================================================

The tier ceiling is a CEILING for sizing (the ``memecoin`` preset tiers:
A ≥ $25k and ≥ 48 h; B ≥ $10k and ≥ 2 h; C ≥ $3k), not a buy signal. A
``fail`` anywhere — or a hard fail on the contract screen — is ``REJECT``.

Token names and symbols never enter this block: it renders numbers, statuses
and our own words only.
"""
from __future__ import annotations

import concurrent.futures as _cf
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

PASS, FAIL, UNKNOWN, INFO = "pass", "fail", "unknown", "info"

#: The lines (086 §3.6; the memecoin preset tiers live here, not in a skill). Module constants, not
#: flags: a mandate preset (W2) tightens them per strategy.
LIQUIDITY_FLOOR_USD = 3_000.0
TIER_B_LIQUIDITY_USD = 10_000.0
TIER_A_LIQUIDITY_USD = 25_000.0
TIER_B_AGE_H = 2.0
TIER_A_AGE_H = 48.0
LP_LOCKED_MIN_PCT = 80.0
SELL_TAX_MAX_PCT = 10.0
TOP_HOLDERS_MAX_PCT = 30.0
BUNDLED_MAX_PCT = 15.0

TIERS = ("A", "B", "C")
REJECT = "REJECT"

#: Addresses that hold burned LP or supply. A burn is not a holder who can sell.
_BURN = {"0x0000000000000000000000000000000000000000",
         "0x000000000000000000000000000000000000dead"}

GATHER_BUDGET_SEC = 10.0
_POOL = _cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="survivability")


def _f(name: str, status: str, value: Any = None, source: Optional[str] = None,
       reason: Optional[str] = None) -> Dict[str, Any]:
    return {"name": name, "status": status, "value": value, "source": source,
            "reason": reason}


def _checks(screen) -> Dict[str, List[Any]]:
    out: Dict[str, List[Any]] = {}
    for c in getattr(screen, "checks", []) or []:
        out.setdefault(c.name, []).append(c)
    return out


def _pct_in(text: str) -> Optional[float]:
    """The first number in a check result, as a float (``"5% (simulated)"``)."""
    import re
    m = re.search(r"-?\d+(?:\.\d+)?", str(text or ""))
    return float(m.group(0)) if m else None


# --------------------------------------------------------------------------
# One field each — pure
# --------------------------------------------------------------------------

def _pool_age(pools, pools_error) -> Dict[str, Any]:
    if pools_error:
        return _f("pool_age_h", UNKNOWN, reason=f"pool list not read ({pools_error})")
    rows = [p for p in (pools or []) if getattr(p, "age_hours", None) is not None]
    if not rows:
        return _f("pool_age_h", UNKNOWN, reason="no pool with a creation time is indexed")
    main = max(rows, key=lambda p: (p.liquidity_usd is not None, p.liquidity_usd or 0.0))
    oldest = max(p.age_hours for p in rows)
    return _f("pool_age_h", INFO, round(main.age_hours, 2), "geckoterminal",
              f"deepest pool; oldest pool {oldest:,.1f} h")


def _liquidity(quote, pools) -> Dict[str, Any]:
    liq = getattr(quote, "liquidity_usd", None)
    src = "price quote"
    if liq is None or liq <= 0:
        known = [p.liquidity_usd for p in (pools or []) if p.liquidity_usd]
        liq, src = (sum(known), "geckoterminal") if known else (None, None)
    if liq is None:
        return _f("liquidity_usd", UNKNOWN, reason="no source reported pool depth")
    status = PASS if liq >= LIQUIDITY_FLOOR_USD else FAIL
    return _f("liquidity_usd", status, round(liq, 2), src,
              None if status == PASS else f"below the ${LIQUIDITY_FLOOR_USD:,.0f} floor")


def _lp_locked(family: str, screen, holders) -> Dict[str, Any]:
    if family == "svm":
        if getattr(screen, "curve", None) is not None:
            return _f("lp_locked_pct", UNKNOWN,
                      reason="on the pump.fun bonding curve — no pool LP exists yet")
        rows = _checks(screen).get("lp_locked_pct") or []
        val = _pct_in(rows[0].result) if rows else None
        if val is None:
            return _f("lp_locked_pct", UNKNOWN, reason="rugcheck did not report LP lock")
        src = "rugcheck"
    else:
        lp = list(getattr(holders, "lp_holders", None) or []) if holders is not None else []
        known = [h for h in lp if h.percent is not None]
        if not known:
            return _f("lp_locked_pct", UNKNOWN, reason="no LP holder rows were reported")
        val = 100.0 * sum(h.percent for h in known
                          if h.is_locked is True or str(h.address).lower() in _BURN)
        src = "goplus"
        if val < LP_LOCKED_MIN_PCT and any(h.is_nft_position for h in known):
            # Measured live 2026-10-23: a 594-day-old Base pool and a Robinhood
            # v4 pool both read 0% locked — their LP is NFT positions, whose
            # lockers GoPlus does not all know. Not seen is not "can be pulled".
            return _f("lp_locked_pct", UNKNOWN, round(val, 2), src,
                      "the LP is concentrated-liquidity positions; GoPlus does not "
                      "see every position locker")
    status = PASS if val >= LP_LOCKED_MIN_PCT else FAIL
    return _f("lp_locked_pct", status, round(val, 2), src,
              None if status == PASS else
              f"under {LP_LOCKED_MIN_PCT:g}% of the LP is locked or burned — it can be pulled")


def _sell_sim(family: str, screen) -> Dict[str, Any]:
    if family == "svm":
        return _f("sell_simulation", UNKNOWN,
                  reason="no keyless sell simulation exists for Solana")
    by = _checks(screen)
    rows = sorted(by.get("is_honeypot", []), key=lambda c: c.source != "honeypot.is")
    # ANY source that saw the sell fail decides it: two sources that disagree
    # on a honeypot are not averaged into a pass.
    bad = [c for c in rows + by.get("cannot_sell_all", []) if str(c.result) == "1"]
    if bad:
        return _f("sell_simulation", FAIL, "sell fails", bad[0].source,
                  "the simulated sell did not go through")
    if rows:
        return _f("sell_simulation", PASS, "sell succeeds", rows[0].source)
    return _f("sell_simulation", UNKNOWN, reason="no source simulated a sell")


def _sell_tax(family: str, screen) -> Dict[str, Any]:
    by = _checks(screen)
    vals = []
    if family == "svm":
        for c in by.get("transfer_fee", []):
            r = str(c.result).lower()
            if r.startswith("unreadable"):
                continue
            vals.append((0.0 if r.startswith(("no", "0 bps")) else _pct_in(r), c.source))
    else:
        for c in by.get("sell_tax", []):
            v = _pct_in(c.result)
            if v is None:
                continue
            # GoPlus sends a FRACTION ("0.05"); honeypot.is a percent ("5% (simulated)").
            vals.append((v * 100.0 if c.source == "goplus" else v, c.source))
    vals = [(v, s) for v, s in vals if v is not None]
    if not vals:
        return _f("sell_tax_pct", UNKNOWN, reason="no source reported a sell tax")
    worst, src = max(vals)
    status = PASS if worst <= SELL_TAX_MAX_PCT else FAIL
    return _f("sell_tax_pct", status, round(worst, 4), src,
              None if status == PASS else f"over {SELL_TAX_MAX_PCT:g}%")


def _authority(name: str, check: str, screen) -> Dict[str, Any]:
    rows = _checks(screen).get(check, [])
    rpc = [c for c in rows if c.source == "rpc"] or rows
    if not rpc:
        return _f(name, UNKNOWN, reason="the mint account was not read")
    r = str(rpc[0].result).lower()
    if r.startswith("no"):
        return _f(name, PASS, "revoked", rpc[0].source)
    return _f(name, FAIL, "set", rpc[0].source,
              "an authority can still mint more" if check == "mintable"
              else "an authority can freeze any holder")


def _owner_powers(screen) -> Dict[str, Any]:
    by = _checks(screen)
    bal = [str(c.result) for c in by.get("owner_change_balance", [])]
    tax = [str(c.result) for c in by.get("slippage_modifiable", [])]
    if "1" in bal or "1" in tax:
        what = " and ".join(w for w, v in (("balances", bal), ("the tax", tax)) if "1" in v)
        return _f("owner_can_change", FAIL, what, "goplus", f"the owner can change {what}")
    if bal and tax:
        return _f("owner_can_change", PASS, "neither", "goplus")
    owner = [str(c.result) for c in by.get("owner", [])]
    hidden = [str(c.result) for c in by.get("hidden_owner", [])]
    if any(o.startswith("renounced") for o in owner) and hidden and all(h == "0" for h in hidden):
        return _f("owner_can_change", PASS, "owner renounced", "rpc")
    return _f("owner_can_change", UNKNOWN,
              reason="no source said whether the owner can change balances or tax")


def _top_holders(holders, screen=None) -> Dict[str, Any]:
    if holders is None or not getattr(holders, "available", False):
        # Jupiter's audit carries an aggregate top-holder share (a PERCENT) —
        # the fallback when the per-address read failed (public Solana RPCs
        # often refuse the largest-accounts call).
        jup = [c for c in _checks(screen).get("top_holders_pct", []) if c.source == "jupiter"]
        val = _pct_in(jup[0].result) if jup else None
        if val is not None:
            status = PASS if val <= TOP_HOLDERS_MAX_PCT else FAIL
            return _f("top_holders_pct", status, round(val, 2), "jupiter",
                      "Jupiter's aggregate top-holder share" if status == PASS
                      else f"the top holders hold over {TOP_HOLDERS_MAX_PCT:g}%")
        return _f("top_holders_pct", UNKNOWN, reason=getattr(holders, "reason", None)
                  or "the holder report was not available")
    rows = [h for h in holders.top_holders
            if h.percent is not None and h.is_contract is not True
            and str(h.address).lower() not in _BURN]
    if not holders.top_holders:
        return _f("top_holders_pct", UNKNOWN, reason="no holder rows were reported")
    val = 100.0 * sum(sorted((h.percent for h in rows), reverse=True)[:10])
    status = PASS if val <= TOP_HOLDERS_MAX_PCT else FAIL
    return _f("top_holders_pct", status, round(val, 2), "holders",
              "top 10 wallets, contracts/programs and burns excluded" if status == PASS
              else f"the top 10 wallets hold over {TOP_HOLDERS_MAX_PCT:g}%")


def _bundled(family: str, insiders, insiders_error) -> Dict[str, Any]:
    if family != "svm":
        return _f("bundled_supply_pct", UNKNOWN,
                  reason="no keyless bundle/insider read exists for EVM chains")
    if insiders_error or insiders is None:
        return _f("bundled_supply_pct", UNKNOWN,
                  reason=f"rugcheck insider graph not read ({insiders_error or 'no answer'})")
    if insiders.networks_pct is None:
        return _f("bundled_supply_pct", UNKNOWN, reason="rugcheck's insider graph did not run")
    val = insiders.networks_pct
    status = PASS if val <= BUNDLED_MAX_PCT else FAIL
    return _f("bundled_supply_pct", status, round(val, 2), "rugcheck",
              f"{insiders.networks} insider network(s) — addresses, not people"
              if status == PASS else f"insider networks hold over {BUNDLED_MAX_PCT:g}%")


# --------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------

def _base_tier(liq: Optional[float], age: Optional[float]) -> str:
    """The ladder, with an unknown AGE treated as met — the unknown penalty is
    applied once, afterwards, so it is never charged twice. Unknown DEPTH is
    C: the ladder is a depth ladder, and nothing can be sized above C on a
    pool nobody measured."""
    if liq is None:
        return "C"
    liq_ok = lambda line: liq >= line  # noqa: E731
    age_ok = lambda line: age is None or age >= line  # noqa: E731
    if liq_ok(TIER_A_LIQUIDITY_USD) and age_ok(TIER_A_AGE_H):
        return "A"
    if liq_ok(TIER_B_LIQUIDITY_USD) and age_ok(TIER_B_AGE_H):
        return "B"
    return "C"


def assess(family: str, *, screen, holders, quote, pools=None, pools_error=None,
           insiders=None, insiders_error=None) -> Dict[str, Any]:
    """Pure. Every field, the unknown and failed names, and the tier ceiling."""
    fields = [
        _pool_age(pools, pools_error),
        _liquidity(quote, pools),
        _lp_locked(family, screen, holders),
        _sell_sim(family, screen),
        _sell_tax(family, screen),
    ]
    if family == "svm":
        fields += [_authority("mint_authority", "mintable", screen),
                   _authority("freeze_authority", "freezable", screen)]
    else:
        fields.append(_owner_powers(screen))
    fields += [_top_holders(holders, screen), _bundled(family, insiders, insiders_error),
               _f("fresh_wallet_ratio", INFO, None, None,
                  "no keyless source; reported, not gated")]
    by = {f["name"]: f for f in fields}
    failed = [f["name"] for f in fields if f["status"] == FAIL]
    hard = list(getattr(screen, "hard_fails", []) or [])
    unknown = [f["name"] for f in fields
               if f["status"] == UNKNOWN or (f["name"] == "pool_age_h" and f["value"] is None)]
    if failed or hard:
        tier = REJECT
    else:
        base = _base_tier(by["liquidity_usd"]["value"], by["pool_age_h"]["value"])
        tier = TIERS[min(TIERS.index(base) + 1, len(TIERS) - 1)] if unknown else base
    return {"tier_ceiling": tier, "failed": failed, "unknown": unknown,
            "screen_hard_fails": hard, "fields": fields}


# --------------------------------------------------------------------------
# Gather — the two reads the screen does not already make
# --------------------------------------------------------------------------

def _why(exc: BaseException) -> str:
    from tools.defi.token_screen import plain_reason
    return plain_reason(f"{type(exc).__name__}: {str(exc)[:80]}")


def gather(chain: str, address: str, family: str, *,
           pools_fn: Optional[Callable] = None, insiders_fn: Optional[Callable] = None,
           budget: float = GATHER_BUDGET_SEC) -> Dict[str, Any]:
    """``{pools, pools_error, insiders, insiders_error}`` inside one budget.
    A read that fails or overruns is named, never dropped and never a pass."""
    from tools.defi.providers import geckoterminal, token_audits
    jobs = {"pools": lambda: (pools_fn or geckoterminal.token_pools)(chain, address)}
    if family == "svm":
        jobs["insiders"] = lambda: (insiders_fn or token_audits.rugcheck_insiders)(address)
    futs = {k: _POOL.submit(fn) for k, fn in jobs.items()}
    done, _ = _cf.wait(list(futs.values()), timeout=budget)
    out: Dict[str, Any] = {"pools": None, "pools_error": None,
                           "insiders": None, "insiders_error": None}
    for k, fut in futs.items():
        if fut not in done:
            fut.cancel()
            out[f"{k}_error"] = f"timed out after {budget:g}s"
            continue
        try:
            out[k] = fut.result()
        except Exception as exc:
            out[f"{k}_error"] = _why(exc)
    return out


def for_token(chain: str, address: str, *, screen, holders, quote,
              gather_fn: Optional[Callable] = None) -> Dict[str, Any]:
    """Gather + assess for ``token_info``. Never raises: a broken gather makes
    every field it fed UNKNOWN, which lowers the tier — it never clears one."""
    from core.wallet import chains
    row = chains.get(chain)
    family = row.family if row is not None else "evm"
    try:
        got = (gather_fn or gather)(chain, address, family)
    except Exception as exc:
        logger.info("survivability: gather failed for %s/%s (%s)", chain, address, exc)
        got = {"pools_error": _why(exc), "insiders_error": _why(exc)}
    return assess(family, screen=screen, holders=holders, quote=quote,
                  pools=got.get("pools"), pools_error=got.get("pools_error"),
                  insiders=got.get("insiders"), insiders_error=got.get("insiders_error"))


def _value_text(f: Dict[str, Any]) -> str:
    v = f["value"]
    if f["name"] == "pool_age_h":
        return "unknown" if v is None else f"{v:,.1f} h"
    if f["name"] == "liquidity_usd":
        return "unknown" if v is None else f"${v:,.0f}"
    if f["name"].endswith("_pct"):
        return "unknown" if v is None else f"{v:,.1f}%"
    return "unknown" if v is None else str(v)


def render(result: Dict[str, Any]) -> List[str]:
    """Two or three lines: the ceiling, the failed gates, the unknown gates."""
    tier = result["tier_ceiling"]
    by = {f["name"]: f for f in result["fields"]}
    head = (f"  survivability: tier ceiling {tier}" if tier != REJECT
            else "  survivability: REJECT")
    head += (f" — pool {_value_text(by['pool_age_h'])} old, "
             f"liquidity {_value_text(by['liquidity_usd'])}")
    lines = [head]
    if result["failed"]:
        lines.append("      failed: " + "; ".join(
            f"{n} {_value_text(by[n])} ({by[n]['reason']})" for n in result["failed"]))
    if result["screen_hard_fails"]:
        lines.append("      the contract screen has a HARD FAIL (above).")
    if result["unknown"]:
        lines.append("      unknown: " + ", ".join(result["unknown"])
                     + " — an unknown is not a pass; it lowers the ceiling one tier.")
    return lines
