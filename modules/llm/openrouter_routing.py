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


# ---------------------------------------------------------------------------
# B-3 (review 2026-10-14, owner approved): `tool_choice` on an agent step.
#
# 13.4 % of agent calls came back as a text-only "empty action" while the
# client always sent `tool_choice:"auto"`. On an agent step every reply IS a
# tool call (`send_message` talks, `done` ends), so the step asks for
# `required`. Aux, no-tools and other callers keep `auto`. An upstream that
# rejects `required` gets ONE retry with `auto`, and the model is remembered
# for the life of the process so the next step does not pay the 400 again.
# ---------------------------------------------------------------------------

_TOOL_CHOICES = frozenset({"auto", "required", "none"})
#: the tool whose presence marks a call as an agent step
AGENT_STEP_TOOL = "done"
_REQUIRED_REJECTED: set = set()


def _tool_names(tools: Any) -> set:
    names = set()
    for t in tools or []:
        if isinstance(t, dict):
            fn = t.get("function") if isinstance(t.get("function"), dict) else t
            name = fn.get("name") if isinstance(fn, dict) else None
            if isinstance(name, str):
                names.add(name)
    return names


def resolve_tool_choice(tools: Any, model: Optional[str],
                        explicit: Optional[str] = None) -> str:
    """The `tool_choice` for a request that carries *tools*.

    *explicit* (``auto``/``required``/``none``) wins; else ``required`` when the
    toolset holds :data:`AGENT_STEP_TOOL` (an agent step), else ``auto``. A
    ``required`` for a model that already rejected it in this process is
    ``auto``.
    """
    value = (explicit or "").strip().lower()
    if value not in _TOOL_CHOICES:
        value = "required" if AGENT_STEP_TOOL in _tool_names(tools) else "auto"
    if value == "required" and (model or "") in _REQUIRED_REJECTED:
        return "auto"
    return value


def is_tool_choice_rejection(exc: BaseException) -> bool:
    """True when a provider error refuses the `tool_choice` value (a 400, or
    OpenRouter's 404 "no endpoints ... tool_choice")."""
    text = str(exc or "").lower()
    return "tool_choice" in text or "tool choice" in text


def remember_required_rejected(model: Optional[str]) -> None:
    """Never ask *model* for ``required`` again in this process."""
    if model:
        _REQUIRED_REJECTED.add(model)


def tool_choice_for(client: Any, tools: Any, explicit: Optional[str] = None) -> str:
    """The client's `tool_choice`. ``required`` on an agent step is OpenRouter's
    own default only: a subclass (NIM, an OpenAI-compatible local server) keeps
    ``auto`` unless the caller asks explicitly."""
    if explicit is None and getattr(client, "_PROVIDER_LABEL", "OpenRouter") != "OpenRouter":
        return "auto"
    return resolve_tool_choice(tools, getattr(client, "model_type", None), explicit)


async def create_with_tool_choice_fallback(client: Any, sdk: Any,
                                           request_params: Dict[str, Any]) -> Any:
    """One `chat.completions.create`. An upstream that refuses
    ``tool_choice:"required"`` is retried ONCE with ``auto``, and the model is
    remembered for the process. Any other error propagates unchanged."""
    try:
        return await sdk.chat.completions.create(**request_params)
    except Exception as e:
        if request_params.get("tool_choice") != "required" or not is_tool_choice_rejection(e):
            raise
        model = getattr(client, "model_type", None)
        remember_required_rejected(model)
        logger = getattr(client, "logger", None)
        if logger is not None:
            logger.warning(f"event=tool_choice_required_rejected model={model} "
                           f"action=retry_auto ({str(e)[:160]})")
        request_params["tool_choice"] = "auto"
        return await sdk.chat.completions.create(**request_params)
