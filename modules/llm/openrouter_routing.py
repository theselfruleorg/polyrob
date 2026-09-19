"""OpenRouter provider routing — `OPENROUTER_PROVIDER_SORT` (2026-09-19).

OpenRouter routes a model to the cheapest upstream by default. On the owner's
metered seat (deepseek-v4.1-flash) that upstream stalled for 290–410 s often
enough to cut five money-rail runs in one afternoon. The knob asks OpenRouter
to order upstreams for the SAME model by `latency`, `throughput` or `price`
instead — an ops knob, not a seat change. Unset (or any other value) sends no
`provider` block at all: byte-identical request. Lives in its own module so
`openrouter_client.py` stays under its file-size ceiling.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

_PROVIDER_SORT_VALUES = frozenset({"latency", "throughput", "price"})


def provider_routing_extra_body() -> Optional[Dict[str, Any]]:
    """The `extra_body` OpenRouter routing block, or None when the knob is unset."""
    raw = (os.getenv("OPENROUTER_PROVIDER_SORT") or "").strip().lower()
    if raw not in _PROVIDER_SORT_VALUES:
        return None
    return {"provider": {"sort": raw}}


def apply_provider_routing(request_params: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the routing block into a chat.completions request (in place)."""
    body = provider_routing_extra_body()
    if body:
        extra = dict(request_params.get("extra_body") or {})
        extra.update(body)
        request_params["extra_body"] = extra
    return request_params
