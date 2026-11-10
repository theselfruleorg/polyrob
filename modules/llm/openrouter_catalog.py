"""The LIVE OpenRouter model list — does this id exist, does it call tools,
what does it cost, how big is its window.

The built-in registry (``model_catalog.py``) is a dated table; an OpenRouter id
newer than the tree falls to a family guess there (a wrong window, a wrong price).
The model switch asks this module instead and, for an id the registry does not
know exactly, registers the live row (``register_live_model``) so the token
budgets and the cost estimate of the swapped session read the real numbers.

One public GET (no key), cached on disk for 24 h under ``<data home>/cache``. A
failed fetch is NOT an empty catalog: :func:`lookup` then answers ``None`` and the
caller says "not verified" — never "does not exist".
"""
from __future__ import annotations

import difflib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

URL = "https://openrouter.ai/api/v1/models"
TTL_S = 24 * 3600
#: a miss re-reads the list at most this often; a failed read is not retried sooner
REFRESH_COOLDOWN_S = 600
_MEM: Dict[str, object] = {"at": 0.0, "rows": None, "fetched": 0.0, "failed": 0.0}


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    input_price: Optional[float]   # USD per million tokens; None = variable/unknown
    output_price: Optional[float]
    cached_price: Optional[float]
    context: int
    tools: bool
    reasoning: bool

    @property
    def price(self) -> Optional[Tuple[float, float]]:
        """None when either price is unknown or variable (a router such as
        ``openrouter/auto`` lists -1): every price rule then fails closed."""
        if self.input_price is None or self.output_price is None:
            return None
        return self.input_price, self.output_price


def _cache_path() -> Path:
    from core.runtime_paths import effective_data_home
    return Path(effective_data_home()) / "cache" / "openrouter_models.json"


def _per_m(value) -> Optional[float]:
    """USD per million tokens, or None for a missing, malformed or negative
    (variable-price) value."""
    try:
        price = round(float(value) * 1e6, 6)
    except (TypeError, ValueError):
        return None
    return price if price >= 0 else None


def _parse(payload: dict) -> Dict[str, CatalogEntry]:
    rows: Dict[str, CatalogEntry] = {}
    for m in (payload or {}).get("data") or []:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        pricing = m.get("pricing") or {}
        params = m.get("supported_parameters") or []
        rows[mid] = CatalogEntry(
            id=mid,
            input_price=_per_m(pricing.get("prompt")),
            output_price=_per_m(pricing.get("completion")),
            cached_price=_per_m(pricing.get("input_cache_read")),
            context=int(m.get("context_length") or 0),
            tools="tools" in params,
            reasoning="reasoning" in params or "include_reasoning" in params,
        )
    return rows


def _fetch() -> dict:
    import httpx
    # Short: a chat seat awaits this (off-thread, but the reply waits) — a slow list
    # falls to "not verified", never to a stuck poll loop.
    with httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0), follow_redirects=True) as c:
        resp = c.get(URL)
        resp.raise_for_status()
        return resp.json()


def catalog(*, refresh: bool = False) -> Optional[Dict[str, CatalogEntry]]:
    """``{id: CatalogEntry}`` or ``None`` when neither the cache nor the network
    can answer (logged). Blocking — call it off the event loop."""
    now = time.time()
    if not refresh and _MEM["rows"] is not None and now - float(_MEM["at"]) < TTL_S:
        return _MEM["rows"]  # type: ignore[return-value]
    if refresh and now - float(_MEM.get("fetched", 0.0)) < REFRESH_COOLDOWN_S:
        return _MEM["rows"]  # type: ignore[return-value]  # re-read recently: no new fetch
    if _MEM["rows"] is None and now - float(_MEM.get("failed", 0.0)) < REFRESH_COOLDOWN_S:
        return None  # the list was unreadable a moment ago: answer fast, do not block
    path = _cache_path()
    if not refresh:
        try:
            if path.exists() and now - path.stat().st_mtime < TTL_S:
                rows = _parse(json.loads(path.read_text(encoding="utf-8")))
                if rows:
                    _MEM.update(at=path.stat().st_mtime, rows=rows)
                    return rows
        except Exception:
            logger.debug("openrouter catalog cache unreadable at %s", path, exc_info=True)
    try:
        _MEM["fetched"] = now
        payload = _fetch()
        rows = _parse(payload)
        if not rows:
            raise ValueError("empty model list")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            logger.debug("openrouter catalog cache not written", exc_info=True)
        _MEM.update(at=now, rows=rows)
        return rows
    except Exception as exc:  # noqa: BLE001 — "could not read" is not "empty"
        logger.warning("openrouter model list unavailable: %s", exc)
        # A stale cache beats nothing: the switch still validates, and says so.
        try:
            if path.exists():
                rows = _parse(json.loads(path.read_text(encoding="utf-8")))
                if rows:
                    _MEM.update(at=now - TTL_S + 600, rows=rows)  # retry in 10 min
                    return rows
        except Exception:
            pass
        _MEM["failed"] = now
        return None


def cached_entry(model_id: str) -> Optional[CatalogEntry]:
    """The entry from memory or the disk cache only — never the network (a session
    recreate re-registers a pinned model's live window with this)."""
    rows = _MEM["rows"]
    if rows is None:
        try:
            path = _cache_path()
            if path.exists():
                rows = _parse(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            rows = None
    return (rows or {}).get(model_id)  # type: ignore[union-attr]


def lookup(model_id: str, *, refresh_on_miss: bool = True) -> Tuple[Optional[CatalogEntry], bool]:
    """``(entry, catalog_read)``. ``(None, True)`` = the id is not on OpenRouter;
    ``(None, False)`` = the list could not be read. A miss re-reads the list once
    (a model released since the cache was written)."""
    rows = catalog()
    if rows is None:
        return None, False
    entry = rows.get(model_id)
    if entry is None and refresh_on_miss:
        fresh = catalog(refresh=True)
        if fresh is not None:
            entry = fresh.get(model_id)
    return entry, True


def nearest(model_id: str, n: int = 3) -> List[str]:
    rows = _MEM["rows"] or {}
    return difflib.get_close_matches(model_id, list(rows), n=n, cutoff=0.5)  # type: ignore[arg-type]


def register_live_model(entry: CatalogEntry) -> None:
    """Teach the registry the live window + price of an OpenRouter id it does not
    know EXACTLY, so budgets and cost estimates stop using a family guess."""
    from modules.llm.model_registry import get_model_config_exact, register_custom_model
    if get_model_config_exact(entry.id) is not None or entry.price is None:
        return  # known exactly, or a variable price (a 0.0 row would bill as free)
    from modules.llm.model_types import (ModelCapabilities, ModelConfig, ModelPricing,
                                         ModelProvider)
    try:
        register_custom_model(ModelConfig(
            name=entry.id, provider=ModelProvider.OPENROUTER,
            context_window=entry.context or 128000,
            max_completion_tokens=min(max(entry.context // 8, 8192), 128000),
            pricing=ModelPricing(input_price=entry.input_price or 0.0,
                                 output_price=entry.output_price or 0.0,
                                 # no listed cached price: a cache hit bills as input
                                 cached_input_price=entry.cached_price or entry.input_price),
            capabilities=ModelCapabilities(supports_function_calling=entry.tools,
                                           supports_thinking=entry.reasoning),
        ))
    except Exception:
        logger.debug("live model %s not registered", entry.id, exc_info=True)
