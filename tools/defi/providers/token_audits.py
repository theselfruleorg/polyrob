"""Keyless token-audit providers for the merged screen (071 W2 §3.6, §4).

Three free sources, each verified LIVE on 2026-10-02 before this was written:

* **Jupiter Tokens v2** (``lite-api.jup.ag/tokens/v2/search?query=<mint>``) —
  Solana. ``audit`` (``mintAuthorityDisabled`` / ``freezeAuthorityDisabled`` /
  ``topHoldersPercentage`` / ``devBalancePercentage``; a key is ABSENT rather
  than false when the condition does not hold), ``organicScore`` (+ label),
  ``isVerified``, ``tags``, ``holderCount``. An empty list = not indexed.
* **RugCheck** (``api.rugcheck.xyz/v1/tokens/<mint>/report/summary``) —
  Solana. Works WITHOUT a key; ``score_normalised`` (higher = riskier) and named
  ``risks`` with a ``level`` (``danger`` / ``warn`` / ``info``). HTTP 400
  ``unable to generate report`` for a mint it does not know.
* **Honeypot.is** (``api.honeypot.is/v2/IsHoneypot?address=&chainID=``) —
  EVM, Ethereum (1) and Base (8453) only here. A real buy-then-sell
  simulation: ``honeypotResult.isHoneypot`` and ``simulationResult``
  buy/sell/transfer tax in PERCENT. ``simulationSuccess: false`` means the
  simulation did not complete — a check that did not run.

Every parser is pure and fails CLOSED: a payload it does not recognise returns
``None`` (unknown), never a pass. The fetchers raise; the caller names the
failure as NOT CHECKED.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

JUPITER_TOKENS_URL = "https://lite-api.jup.ag/tokens/v2/search?query={mint}"
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
HONEYPOT_URL = "https://api.honeypot.is/v2/IsHoneypot?address={address}&chainID={chain_id}"

#: The chains Honeypot.is simulates that this registry also carries. Every
#: other chain is NOT CHECKED by name, never quietly skipped.
HONEYPOT_CHAINS = {"ethereum": 1, "base": 8453}

TIMEOUT_SEC = 8.0


def _num(raw: Any) -> Optional[float]:
    if isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Jupiter Tokens v2
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class JupiterToken:
    mint: str
    symbol: Optional[str] = None
    is_verified: Optional[bool] = None
    tags: List[str] = field(default_factory=list)
    organic_score: Optional[float] = None
    organic_label: Optional[str] = None
    mint_authority_disabled: Optional[bool] = None
    freeze_authority_disabled: Optional[bool] = None
    top_holders_pct: Optional[float] = None     # PERCENT (26.2 = 26.2 %)
    dev_balance_pct: Optional[float] = None     # PERCENT
    holder_count: Optional[int] = None
    launchpad: Optional[str] = None


def _authority_disabled(audit: Dict[str, Any], key: str, top: Dict[str, Any],
                        top_key: str) -> Optional[bool]:
    """``audit.<key>`` is present only when TRUE; the top-level authority field
    is the cross-check. Both silent = unknown."""
    if audit.get(key) is True:
        return True
    if top_key in top:
        return not bool(top.get(top_key))
    return None


def parse_jupiter(mint: str, payload: Any) -> Optional[JupiterToken]:
    """The row for *mint* (exact, case-sensitive), or None when not indexed."""
    if not isinstance(payload, list):
        raise ValueError("unexpected Jupiter tokens payload")
    row = next((r for r in payload if isinstance(r, dict) and r.get("id") == mint), None)
    if row is None:
        return None
    audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
    hc = _num(row.get("holderCount"))
    tags = [str(t) for t in (row.get("tags") or []) if isinstance(t, str)]
    return JupiterToken(
        mint=mint,
        symbol=row.get("symbol") if isinstance(row.get("symbol"), str) else None,
        is_verified=row.get("isVerified") if isinstance(row.get("isVerified"), bool) else None,
        tags=tags,
        organic_score=_num(row.get("organicScore")),
        organic_label=(str(row.get("organicScoreLabel")) if row.get("organicScoreLabel") else None),
        mint_authority_disabled=_authority_disabled(audit, "mintAuthorityDisabled", row, "mintAuthority"),
        freeze_authority_disabled=_authority_disabled(audit, "freezeAuthorityDisabled", row, "freezeAuthority"),
        top_holders_pct=_num(audit.get("topHoldersPercentage")),
        dev_balance_pct=_num(audit.get("devBalancePercentage")),
        holder_count=None if hc is None else int(hc),
        launchpad=(str(row.get("launchpad")) if row.get("launchpad") else None),
    )


# --------------------------------------------------------------------------
# RugCheck
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RugCheckRisk:
    name: str
    level: Optional[str] = None
    value: Optional[str] = None


@dataclass(frozen=True)
class RugCheckReport:
    score_normalised: Optional[float]
    risks: List[RugCheckRisk] = field(default_factory=list)
    lp_locked_pct: Optional[float] = None

    @property
    def danger(self) -> List[RugCheckRisk]:
        return [r for r in self.risks if (r.level or "").lower() == "danger"]


def parse_rugcheck(payload: Any) -> RugCheckReport:
    if not isinstance(payload, dict) or "risks" not in payload:
        raise ValueError("unexpected RugCheck payload")
    if not isinstance(payload.get("risks"), list):
        raise ValueError("RugCheck risks is not a list")
    risks = []
    for r in payload["risks"]:
        if isinstance(r, dict) and r.get("name"):
            risks.append(RugCheckRisk(
                name=str(r["name"]),
                level=(str(r["level"]) if r.get("level") else None),
                value=(str(r["value"]) if r.get("value") not in (None, "") else None)))
    score = _num(payload.get("score_normalised"))
    if score is None and not risks:
        raise ValueError("RugCheck sent neither a score nor risks")
    return RugCheckReport(score_normalised=score, risks=risks,
                          lp_locked_pct=_num(payload.get("lpLockedPct")))


# --------------------------------------------------------------------------
# Honeypot.is
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class HoneypotSim:
    simulated: bool
    is_honeypot: Optional[bool] = None
    buy_tax_pct: Optional[float] = None
    sell_tax_pct: Optional[float] = None
    transfer_tax_pct: Optional[float] = None
    reason: Optional[str] = None
    pair: Optional[str] = None
    pair_liquidity_usd: Optional[float] = None
    is_proxy: Optional[bool] = None


def parse_honeypot(payload: Any) -> HoneypotSim:
    if not isinstance(payload, dict):
        raise ValueError("unexpected Honeypot.is payload")
    hp = payload.get("honeypotResult") if isinstance(payload.get("honeypotResult"), dict) else {}
    sim = payload.get("simulationResult") if isinstance(payload.get("simulationResult"), dict) else {}
    pair = payload.get("pair") if isinstance(payload.get("pair"), dict) else {}
    code = payload.get("contractCode") if isinstance(payload.get("contractCode"), dict) else {}
    ok = payload.get("simulationSuccess") is True
    reason = None
    if not ok:
        reason = str(payload.get("simulationError") or "simulation did not complete")
    return HoneypotSim(
        simulated=ok,
        is_honeypot=hp.get("isHoneypot") if isinstance(hp.get("isHoneypot"), bool) else None,
        buy_tax_pct=_num(sim.get("buyTax")) if ok else None,
        sell_tax_pct=_num(sim.get("sellTax")) if ok else None,
        transfer_tax_pct=_num(sim.get("transferTax")) if ok else None,
        reason=reason,
        pair=(str(payload.get("pairAddress")) if payload.get("pairAddress") else None),
        pair_liquidity_usd=_num(pair.get("liquidity")),
        is_proxy=code.get("isProxy") if isinstance(code.get("isProxy"), bool) else None,
    )


# --------------------------------------------------------------------------
# Network boundary — blocked in unit tests.
# --------------------------------------------------------------------------

class SourceUnavailable(RuntimeError):
    """The provider answered, but not with a usable report (a named reason)."""


def _get(url: str):
    """(status, json-or-None). Pooled client — never a fresh connection."""
    from tools.defi.providers import _http
    try:
        return 200, _http.get_json(url, timeout=TIMEOUT_SEC)
    except _http.HttpStatusError as exc:
        return exc.code, None


def jupiter_token(mint: str) -> Optional[JupiterToken]:
    status, body = _get(JUPITER_TOKENS_URL.format(mint=quote(mint, safe="")))
    if status != 200:
        raise SourceUnavailable(f"HTTP {status}")
    return parse_jupiter(mint, body)


def rugcheck_report(mint: str) -> RugCheckReport:
    status, body = _get(RUGCHECK_URL.format(mint=quote(mint, safe="")))
    if status == 400 and isinstance(body, dict) and body.get("error"):
        raise SourceUnavailable(f"no report ({body.get('error')})")
    if status != 200:
        raise SourceUnavailable(f"HTTP {status}")
    return parse_rugcheck(body)


def honeypot_sim(chain: str, address: str) -> HoneypotSim:
    chain_id = HONEYPOT_CHAINS.get(chain)
    if chain_id is None:
        raise SourceUnavailable("chain not covered")
    status, body = _get(HONEYPOT_URL.format(address=quote(address, safe=""), chain_id=chain_id))
    if status != 200:
        detail = body.get("error") if isinstance(body, dict) else None
        raise SourceUnavailable(f"HTTP {status}" + (f" ({detail})" if detail else ""))
    return parse_honeypot(body)
