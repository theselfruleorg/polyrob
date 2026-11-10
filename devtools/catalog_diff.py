"""Diff the built-in model catalog against the live OpenRouter list.

    python devtools/catalog_diff.py

Reads ``GET https://openrouter.ai/api/v1/models`` (public, no key, not an LLM
call). A row whose price differs from the list also reads
``/api/v1/models/<id>/endpoints``: the list shows one upstream, and a price is
wrong only when no upstream serves it. Only OpenRouter rows are compared.

Exit code: 0 clean, 1 at least one dead or wrong row, 2 the list is unreadable.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LIST_URL = "https://openrouter.ai/api/v1/models"
ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{}/endpoints"
#: A window within this fraction of the list is the same window (upstreams round it).
WINDOW_TOLERANCE = 0.05
#: A price within this fraction of an upstream's price is that price.
PRICE_TOLERANCE = 0.02

Range = Tuple[float, float]


@dataclass(frozen=True)
class Finding:
    model: str
    kind: str  # "dead" | "wrong"
    detail: str

    def __str__(self) -> str:
        return f"{self.kind:<5} {self.model}: {self.detail}"


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (fixed https URL)
        return json.load(resp)


def _per_m(value) -> Optional[float]:
    """USD per million tokens, or None for a missing or variable price."""
    try:
        price = float(value) * 1e6
    except (TypeError, ValueError):
        return None
    return round(price, 6) if price >= 0 else None


def fetch_endpoint_prices(model_id: str) -> Dict[str, Range]:
    """The (lowest, highest) served price per field, over every upstream of *model_id*."""
    data = _get_json(ENDPOINTS_URL.format(urllib.parse.quote(model_id, safe="/")))
    seen: Dict[str, List[float]] = {"prompt": [], "completion": [], "input_cache_read": []}
    for endpoint in (data.get("data") or {}).get("endpoints") or []:
        for key, values in seen.items():
            price = _per_m((endpoint.get("pricing") or {}).get(key))
            if price is not None:
                values.append(price)
    return {key: (min(values), max(values)) for key, values in seen.items() if values}


def _outside(price: float, served: Range) -> bool:
    low, high = served
    return price < low * (1 - PRICE_TOLERANCE) or price > high * (1 + PRICE_TOLERANCE)


def _close(price: float, listed: Optional[float]) -> bool:
    return listed is not None and abs(price - listed) <= listed * PRICE_TOLERANCE


def _price_findings(row, live: dict,
                    endpoints_of: Callable[[str], Dict[str, Range]]) -> List[Finding]:
    fields = (("prompt", "input", row.pricing.input_price),
              ("completion", "output", row.pricing.output_price),
              ("input_cache_read", "cached", row.pricing.cached_input_price))
    listed = live.get("pricing") or {}
    if all(_close(price, _per_m(listed.get(key))) for key, _, price in fields):
        return []
    try:
        served = endpoints_of(row.name)
    except OSError as exc:
        return [Finding(row.name, "wrong", f"price differs from the list and the "
                                           f"upstreams are unreadable: {exc}")]
    out = []
    for key, label, price in fields:
        if key in served and _outside(price, served[key]):
            low, high = served[key]
            out.append(Finding(row.name, "wrong", f"{label} price {price:g} is outside "
                                                  f"the served range {low:g}-{high:g}"))
    return out


def diff_rows(rows: Iterable, live_rows: Dict[str, dict],
              endpoints_of: Callable[[str], Dict[str, Range]]) -> List[Finding]:
    """Every dead or wrong row. *rows* are ``ModelConfig``; *live_rows* maps id to list row."""
    findings: List[Finding] = []
    for row in rows:
        live = live_rows.get(row.name)
        if live is None:
            findings.append(Finding(row.name, "dead", "not in the live list"))
            continue
        window = live.get("context_length") or 0
        if window and abs(row.context_window - window) > window * WINDOW_TOLERANCE:
            findings.append(Finding(row.name, "wrong",
                                    f"context {row.context_window} vs live {window}"))
        output = (live.get("top_provider") or {}).get("max_completion_tokens") or 0
        if output and row.max_completion_tokens > output:
            findings.append(Finding(row.name, "wrong",
                                    f"max output {row.max_completion_tokens} vs live {output}"))
        sees = "image" in ((live.get("architecture") or {}).get("input_modalities") or [])
        if row.capabilities.supports_vision != sees:
            findings.append(Finding(row.name, "wrong",
                                    f"vision {row.capabilities.supports_vision} vs live {sees}"))
        calls = "tools" in (live.get("supported_parameters") or [])
        if row.capabilities.supports_tools and not calls:
            findings.append(Finding(row.name, "wrong", "supports_tools but the live row has no tools"))
        findings.extend(_price_findings(row, live, endpoints_of))
    return findings


def main() -> int:
    from modules.llm.model_registry import ModelProvider, get_all_models
    try:
        live_rows = {row["id"]: row for row in _get_json(LIST_URL)["data"]}
    except (OSError, ValueError, KeyError) as exc:
        print(f"the live list is unreadable: {exc}")
        return 2
    rows = get_all_models(ModelProvider.OPENROUTER, include_deprecated=True)
    findings = diff_rows(rows, live_rows, fetch_endpoint_prices)
    for finding in findings:
        print(finding)
    print(f"{len(rows)} OpenRouter rows, {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
