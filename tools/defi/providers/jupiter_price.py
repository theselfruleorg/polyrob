"""Jupiter Price API v3 — a keyless Solana price source (071 §3.3).

``GET https://lite-api.jup.ag/price/v3?ids=<mint>,<mint>`` (measured
2026-10-02: ``{mint: {usdPrice, liquidity, decimals, ...}}``; a mint Jupiter
does not price is simply absent). A DATA source only: this module adds no
route — the routing stack (univ3 -> LI.FI -> Jupiter swap) is unchanged.

Parsing is separate from fetching so unit tests never reach the network
(``tests/unit/tools/defi/conftest.py`` blocks ``_get``).
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

PRICE_URL = "https://lite-api.jup.ag/price/v3"
TIMEOUT_SEC = 8.0
#: Mints per call (the API documents 50).
BATCH_MAX = 50

name = "jupiter"


def _positive(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if (math.isfinite(out) and out > 0) else None


def parse_prices(payload: Any, mints) -> Dict[str, float]:
    """``{mint: usd}`` for each requested mint Jupiter priced. Base58 is
    case-sensitive, so keys match exactly. A missing / zero price is absent."""
    out: Dict[str, float] = {}
    if not isinstance(payload, dict):
        return out
    for mint in mints:
        row = payload.get(mint)
        if isinstance(row, dict):
            val = _positive(row.get("usdPrice"))
            if val is not None:
                out[mint] = val
    return out


def _get(url: str) -> Any:
    from tools.defi.providers import _http
    return _http.get_json(url, timeout=TIMEOUT_SEC)


def prices(mints) -> Dict[str, float]:
    """Batched USD prices. RAISES on a provider error so the caller can name
    the failure; "no price" is an absent key, never 0."""
    # base58 only: a mint reaches the URL as a query value, so anything that
    # is not alphanumeric is refused rather than escaped.
    wanted = [m for m in dict.fromkeys(str(x) for x in mints if x) if m.isalnum()]
    out: Dict[str, float] = {}
    for i in range(0, len(wanted), BATCH_MAX):
        chunk = wanted[i:i + BATCH_MAX]
        out.update(parse_prices(_get(f"{PRICE_URL}?ids={','.join(chunk)}"), chunk))
    return out
