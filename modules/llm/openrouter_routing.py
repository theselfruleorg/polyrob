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


#: attribute holding a PER-CLIENT sort override (057 WS-B, timeout reroute).
_SORT_ATTR = "_polyrob_provider_sort"


def set_provider_sort(client: Any, sort: Optional[str]) -> bool:
    """Override the routing sort for ONE client instance (None clears it).

    057 WS-B: after two consecutive timeouts in a session the runner asks for
    `latency` for the rest of that run — a per-session decision, so it must not
    be written into the process-wide env where it would outlive the run and
    silently re-route every other session.
    """
    value = (sort or "").strip().lower() or None
    if value is not None and value not in _PROVIDER_SORT_VALUES:
        return False
    try:
        setattr(client, _SORT_ATTR, value)
    except Exception:
        return False
    return True


def resolve_provider_sort(client: Any = None) -> Optional[str]:
    """The sort in force: the per-client override, else `OPENROUTER_PROVIDER_SORT`."""
    if client is not None:
        override = getattr(client, _SORT_ATTR, None)
        if isinstance(override, str) and override in _PROVIDER_SORT_VALUES:
            return override
    raw = (os.getenv("OPENROUTER_PROVIDER_SORT") or "").strip().lower()
    return raw if raw in _PROVIDER_SORT_VALUES else None


def provider_routing_extra_body(client: Any = None) -> Optional[Dict[str, Any]]:
    """The `extra_body` OpenRouter routing block, or None when the knob is unset."""
    sort = resolve_provider_sort(client)
    return {"provider": {"sort": sort}} if sort else None


def apply_provider_routing(request_params: Dict[str, Any],
                           client: Any = None) -> Dict[str, Any]:
    """Merge the routing block into a chat.completions request (in place)."""
    body = provider_routing_extra_body(client)
    if body:
        extra = dict(request_params.get("extra_body") or {})
        extra.update(body)
        request_params["extra_body"] = extra
    return request_params
