"""OpenRouter provider routing — `OPENROUTER_PROVIDER_SORT`.

OpenRouter routes a model to the cheapest upstream by default. On the owner's
metered seat (deepseek-v4.1-flash) that upstream stalled for 290–410 s often
enough to cut five money-rail runs in one afternoon. The knob asks OpenRouter
to order upstreams for the SAME model by `latency`, `throughput` or `price`
instead — an ops knob, not a seat change. Unset (or any other value) sends no
`provider` block at all: byte-identical request. Lives in its own module so
`openrouter_client.py` stays under its file-size ceiling.

`OPENROUTER_PROVIDER_ORDER` and `OPENROUTER_PROVIDER_IGNORE` name upstreams.
The same model id behaved differently per upstream: one
served a placeholder post and repeated owner messages on a replay where others
did the next step, and a run moved between upstreams from step to step. The
order is a preference, so a model those upstreams do not serve still answers.

The order takes a plain list for every model, a per-model form, or both:
``DeepInfra,Together`` or ``deepseek/deepseek-v4-pro=Nebius;z-ai/glm-5.3=Z.AI,DeepInfra;Together``.
A model with no entry of its own takes the plain list, and with no plain list no order.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from modules.llm.call_scope import scoped_provider_sort, session_tag, set_scoped_provider_sort
from modules.llm.catalog_lookup import catalog_wire_id
from modules.llm.model_types import model_family
from modules.llm.tool_schemas import schema_tool_names

_PROVIDER_SORT_VALUES = frozenset({"latency", "throughput", "price"})


def _wire_id(name: Optional[str]) -> str:
    return (catalog_wire_id(name, "openrouter") or "").lower()


def set_provider_sort(sort: Optional[str]) -> bool:
    """Scope a routing sort to this task's next calls (None clears it).

    After two timeouts in a row a session asks for ``latency``. That is the
    decision of one session, and the client serves them all.
    """
    value = (sort or "").strip().lower() or None
    if value is not None and value not in _PROVIDER_SORT_VALUES:
        return False
    set_scoped_provider_sort(value)
    return True


def resolve_provider_sort() -> Optional[str]:
    """The sort in force: this task's, else `OPENROUTER_PROVIDER_SORT`."""
    scoped = scoped_provider_sort()
    if scoped:
        return scoped
    raw = (os.getenv("OPENROUTER_PROVIDER_SORT") or "").strip().lower()
    return raw if raw in _PROVIDER_SORT_VALUES else None


def _names(text: str) -> List[str]:
    return [n.strip() for n in text.split(",") if n.strip()]


def _provider_order(model: Optional[str]) -> List[str]:
    """`OPENROUTER_PROVIDER_ORDER` for *model* (see the module docstring)."""
    plain: List[str] = []
    want = _wire_id(model)
    for part in (os.getenv("OPENROUTER_PROVIDER_ORDER") or "").split(";"):
        key, sep, names = part.partition("=")
        if not sep:
            plain = _names(key) or plain
        elif want and _wire_id(key.strip()) == want:
            return _names(names)
    return plain


def provider_routing_extra_body(model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The `extra_body` OpenRouter routing block, or None when every knob is unset."""
    provider: Dict[str, Any] = {}
    sort = resolve_provider_sort()
    if sort:
        provider["sort"] = sort
    order = _provider_order(model)
    if order:
        provider["order"] = order
    ignore = _names(os.getenv("OPENROUTER_PROVIDER_IGNORE") or "")
    if ignore:
        provider["ignore"] = ignore
    return {"provider": provider} if provider else None


def apply_provider_routing(request_params: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the routing block into a chat.completions request (in place)."""
    body = provider_routing_extra_body(request_params.get("model"))
    if body:
        extra = dict(request_params.get("extra_body") or {})
        extra.update(body)
        request_params["extra_body"] = extra
    return request_params


def apply_session_id(request_params: Dict[str, Any]) -> Dict[str, Any]:
    """Send this task's session digest on a GLM model, whose upstream keeps a cache per
    ``session_id``. In place."""
    tag = session_tag()
    if tag and model_family(request_params.get("model")) == "glm":
        extra = dict(request_params.get("extra_body") or {})
        extra["session_id"] = tag
        request_params["extra_body"] = extra
    return request_params


# ---------------------------------------------------------------------------
# `tool_choice` on an agent step.
#
# With `tool_choice:"auto"` an agent call can come back as a text-only
# "empty action" (13.4 % of calls measured). On an agent step every reply IS a
# tool call (`send_message` talks, `done` ends), so the step asks for
# `required`. Aux, no-tools and other callers keep `auto`. An upstream that
# rejects `required` gets ONE retry with `auto`, and the model is remembered
# for the life of the process so the next step does not pay the 400 again.
# ---------------------------------------------------------------------------

_TOOL_CHOICES = frozenset({"auto", "required", "none"})
#: the tool whose presence marks a call as an agent step
AGENT_STEP_TOOL = "done"
#: model -> when it refused ``required``. The refusal is remembered for LATCH_TTL_SEC, not
#: for the process: OpenRouter's 404 "no endpoints … tool_choice" can mean only that the
#: upstreams that support it are down now.
_REQUIRED_REJECTED: Dict[str, float] = {}
LATCH_TTL_SEC = 3600.0


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
        value = "required" if AGENT_STEP_TOOL in schema_tool_names(tools) else "auto"
    if value == "required" and _required_rejected(model):
        return "auto"
    return value


def is_tool_choice_rejection(exc: BaseException) -> bool:
    """True when a provider error refuses the `tool_choice` value (a 400, or
    OpenRouter's 404 "no endpoints ... tool_choice")."""
    text = str(exc or "").lower()
    return "tool_choice" in text or "tool choice" in text


def _required_rejected(model: Optional[str]) -> bool:
    at = _REQUIRED_REJECTED.get(model or "")
    return at is not None and time.monotonic() - at < LATCH_TTL_SEC


def remember_required_rejected(model: Optional[str]) -> None:
    """Do not ask *model* for ``required`` for the next LATCH_TTL_SEC."""
    if model:
        _REQUIRED_REJECTED[model] = time.monotonic()


def tool_choice_for(client: Any, tools: Any, explicit: Optional[str] = None) -> str:
    """The client's `tool_choice`. A client's ``DEFAULT_TOOL_CHOICE`` holds unless the
    caller asks explicitly; with none, an agent step asks for ``required``."""
    default = getattr(client, "DEFAULT_TOOL_CHOICE", None)
    if explicit is None and default:
        return default
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
