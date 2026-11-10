"""The THREE late-tool modes and ONE resolver.

``load_tool`` (and an MCP connect) registers new actions mid-session. That
grows the emitted ``tools[]`` in the ``grow`` mode, and every provider cache serves only the
leading bytes two requests share — on Anthropic the wire order is
``tools -> system -> messages``, so a grown tool array colds the ENTIRE request.

Three answers exist and this module names all three:

``grow``
    The DEFAULT: the emitted
    list grows on ``load_tool``. Provider-neutral, costs one cold request.

``bridge``
    Shape (a), the schema-freeze bridge — opt-in behind ``TOOL_SCHEMAS_FROZEN``. The
    emitted list is pinned at the first emit and a late action is reached by
    name through ``tool_call(name, arguments)``
    (``tools/controller/tool_call_bridge.py``). It trades NATIVE schema
    validation for a byte-stable tools array on every provider.

``deferred``
    Shape (b), Anthropic's native mid-conversation tool changes — the default on
    a capable Anthropic model (``ANTHROPIC_DEFERRED_TOOLS``, default ON). A late
    tool IS emitted, carrying ``"defer_loading": true``: the request KNOWS the
    tool but the model's context does not hold it, so adding one is explicitly
    not a prefix edit. The tool is surfaced later by appending a system-role
    message to ``messages`` carrying ``tool_addition`` blocks. Keeps native
    schema validation AND the cached prefix.

⚠️ NOT LIVE-TESTED. This tree has no Anthropic key, so the deferred path has
never issued a real request. It is therefore built to FAIL SOFT: a 400 that
mentions the deferral vocabulary marks the client unavailable, rebuilds the
request without any deferral and retries once. Any other error propagates
unchanged.

Contract (Anthropic API reference):

* beta header ``anthropic-beta: mid-conversation-tool-changes-2026-07-01``;
  available on the rows that set ``supports_mid_conversation_tool_changes``.
* ``"defer_loading": true`` on a tool in ``tools[]``; at least one tool must be
  non-deferred.
* to surface one, append
  ``{"role": "system", "content": [{"type": "tool_addition",
  "tool": {"type": "tool_reference", "name": "<tool>"}}]}`` to ``messages``.
  A system message inside ``messages`` must follow a ``user`` message, must be
  the LAST entry or be followed by an ``assistant`` turn, and may never be
  ``messages[0]``.
* plain dicts throughout — the SDK typings lag the beta.
"""
from __future__ import annotations
from modules.llm.anthropic_wire import (
    BETA_HEADER_NAME, is_http_400, retry_params_without_thinking_controls, with_beta, without_beta)
from modules.llm.billing_guard import inference_sdk

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: The three late-tool modes. ``late_tool_mode`` returns exactly one of these.
LATE_TOOL_MODE_GROW = "grow"
LATE_TOOL_MODE_BRIDGE = "bridge"
LATE_TOOL_MODE_DEFERRED = "deferred"

#: The beta this rides. Sent as ``extra_headers`` on the create/stream call.
DEFERRED_TOOLS_BETA = "mid-conversation-tool-changes-2026-07-01"

#: The per-tool key that tells Anthropic "known to the request, not in context".
DEFER_KEY = "defer_loading"

#: The envelope the agent tier pushes (``MessageOrigin.TOOL_ADDITION``) and this
#: module parses back into ``tool_addition`` blocks. The body is exactly one
#: action name per line so the parse can never be ambiguous.
ADDITION_OPEN = "<tool-addition>"
ADDITION_CLOSE = "</tool-addition>"

#: Stamped on a CLIENT the first time the API refuses the deferral vocabulary.
#: Same attribute-stamp pattern as ``cache_hints.apply_cache_ttl``.
_UNAVAILABLE_ATTR = "_polyrob_deferred_tools_unavailable"

#: One rebuild for the deferred tools and one for the thinking controls.
_MAX_SELF_HEALS = 2

#: Substrings that identify a 400 as "this endpoint does not know the beta".
_REFUSAL_MARKERS = (DEFER_KEY, "tool_addition", "tool_reference", DEFERRED_TOOLS_BETA)


# ---------------------------------------------------------------------------
# the ONE resolver
# ---------------------------------------------------------------------------

def late_tool_mode(provider: str) -> str:
    """Which late-tool shape this request uses: ``grow`` | ``bridge`` | ``deferred``.

    Precedence is deliberate: an operator who turns ``TOOL_SCHEMAS_FROZEN`` on
    asked for the provider-neutral bridge and gets it everywhere, including on
    Anthropic. Otherwise a capable Anthropic seat defers, and everything else
    grows.
    """
    from core.env import bool_env
    if bool_env("TOOL_SCHEMAS_FROZEN", False):
        return LATE_TOOL_MODE_BRIDGE
    if (provider or "").strip().lower() == "anthropic" and bool_env(
            "ANTHROPIC_DEFERRED_TOOLS", True):
        return LATE_TOOL_MODE_DEFERRED
    return LATE_TOOL_MODE_GROW


def model_supports_deferred_tools(model_name: Optional[str]) -> bool:
    """Does this model accept mid-conversation tool changes? Fail-open to False.

    Reads the ONE capability table (``ModelCapabilities`` in the registry), so a
    new model is declared in exactly one place. An unknown id resolves through
    the registry's family fallback, which lands on a row whose capability flag
    answers honestly for that tier.
    """
    if not model_name:
        return False
    try:
        from modules.llm.model_registry import get_model_config
        config = get_model_config(model_name)
        caps = getattr(config, "capabilities", None)
        return bool(getattr(caps, "supports_mid_conversation_tool_changes", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# the unavailable stamp (the self-heal's memory)
# ---------------------------------------------------------------------------

def deferred_tools_unavailable(client: Any) -> bool:
    """True once this client has been refused the deferral vocabulary."""
    if client is None:
        return False
    if getattr(client, _UNAVAILABLE_ATTR, False):
        return True
    inner = getattr(client, "_client", None)
    return bool(getattr(inner, _UNAVAILABLE_ATTR, False)) if inner is not None else False


def stamp_deferred_unavailable(client: Any) -> None:
    """Remember the refusal on the client (and the SDK object it wraps)."""
    try:
        setattr(client, _UNAVAILABLE_ATTR, True)
        inner = getattr(client, "_client", None)
        if inner is not None:
            setattr(inner, _UNAVAILABLE_ATTR, True)
    except Exception:  # pragma: no cover - defensive
        pass


def deferred_tools_enabled(client: Any) -> bool:
    """May THIS client send a deferred-tools request right now?

    Three gates, all of which must hold: the class supports it (the real
    Anthropic API, not an Anthropic-compat seat), the model supports it, and
    nothing has already been refused on this client.
    """
    if client is None or not getattr(client, "_SUPPORTS_DEFERRED_TOOLS", False):
        return False
    if deferred_tools_unavailable(client):
        return False
    return model_supports_deferred_tools(getattr(client, "model_type", None))


def is_deferred_tools_error(exc: Any) -> bool:
    """Is *exc* a 400 that names the deferral vocabulary? A 400 is required so an unrelated
    failure that happens to quote a tool name can never disarm the feature."""
    if not is_http_400(exc):
        return False
    text = str(exc).lower()
    return any(marker.lower() in text for marker in _REFUSAL_MARKERS)


# ---------------------------------------------------------------------------
# the wire transform
# ---------------------------------------------------------------------------

def _block_text(content: Any) -> Optional[str]:
    """The plain text of a wire message content, or None if it is not all text.

    Handles both shapes the Anthropic path produces: a bare ``str`` and the
    list-of-text-blocks form ``mark_conversation_cache`` rewrites it into when a
    cache breakpoint lands on the message.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        parts = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                return None
            parts.append(block.get("text") or "")
        return "".join(parts)
    return None


def _addition_names(message: Any) -> Optional[List[str]]:
    """The action names a ``<tool-addition>`` message carries, else None.

    Only a message whose content is EXACTLY the envelope with one bare name per
    line qualifies — anything else is ordinary conversation and is left alone.
    """
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    text = _block_text(message.get("content"))
    if not text:
        return None
    text = text.strip()
    if not text.startswith(ADDITION_OPEN) or not text.endswith(ADDITION_CLOSE):
        return None
    body = text[len(ADDITION_OPEN):-len(ADDITION_CLOSE)]
    names = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line.split()) != 1:
            return None  # not a bare name list — do not touch this message
        names.append(line)
    return names or None


def _strip_deferral(tools: Any) -> Tuple[Any, bool]:
    """A copy of *tools* with every ``defer_loading`` key removed.

    ⚠️ NEVER mutates the input: ``tools`` is the registry's MEMOIZED per-provider
    schema list, shared across every model on this process (the same no-mutate
    rule as ``cache_hints.apply_openrouter_tools_cache_control``).
    """
    if not isinstance(tools, list) or not tools:
        return tools, False
    if not any(isinstance(t, dict) and DEFER_KEY in t for t in tools):
        return tools, False
    out = []
    for tool in tools:
        if isinstance(tool, dict) and DEFER_KEY in tool:
            tool = {k: v for k, v in tool.items() if k != DEFER_KEY}
        out.append(tool)
    return out, True


def _deferred_count(tools: Any) -> int:
    if not isinstance(tools, list):
        return 0
    return sum(1 for t in tools if isinstance(t, dict) and t.get(DEFER_KEY))


def _addition_message(names: Sequence[str]) -> Dict[str, Any]:
    return {
        "role": "system",
        "content": [
            {"type": "tool_addition", "tool": {"type": "tool_reference", "name": n}}
            for n in names
        ],
    }


def _is_addition_system(message: Any) -> bool:
    """A system message this module already built (the self-heal's input)."""
    if not isinstance(message, dict) or message.get("role") != "system":
        return False
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    return all(isinstance(b, dict) and b.get("type") in ("tool_addition", "tool_removal")
               for b in content)


def _disable(api_params: Dict[str, Any]) -> Dict[str, Any]:
    """The un-deferred request: no ``defer_loading``, no envelopes, no header.

    This is the plain request, and it is what
    the self-heal retries with. It accepts BOTH shapes — the raw
    ``<tool-addition>`` envelope and the system message this module already
    built from it — because the retry rebuilds an ALREADY-transformed request.
    """
    out = dict(api_params)
    tools, _ = _strip_deferral(out.get("tools"))
    if tools is not out.get("tools"):
        out["tools"] = tools
    messages = out.get("messages")
    if isinstance(messages, list) and any(
            _addition_names(m) or _is_addition_system(m) for m in messages):
        out["messages"] = [m for m in messages
                           if not (_addition_names(m) or _is_addition_system(m))]
    return without_beta(out, DEFERRED_TOOLS_BETA)


def prepare_deferred_tools(api_params: Dict[str, Any], *, enabled: bool) -> Dict[str, Any]:
    """Turn a built request into its deferred-tools form, or strip every trace.

    ``enabled=False`` is the ONE fallback shape: no ``defer_loading`` anywhere,
    the ``<tool-addition>`` envelopes dropped from the wire entirely (the tools
    they name are in ``tools[]`` un-deferred, so the note would be noise), and no
    beta header. Byte-identical to a request built without any of this.

    ``enabled=True`` converts each envelope into a system-role ``tool_addition``
    message, enforces Anthropic's placement rules, guarantees at least one
    non-deferred tool remains, and asks for the beta header.

    Never mutates *api_params* or any list it holds.
    """
    if not enabled:
        return _disable(api_params)

    out = dict(api_params)
    messages = out.get("messages")
    if not isinstance(messages, list):
        return _disable(api_params)

    # At least one tool must be non-deferred. If the freeze armed before ANY
    # tool was emitted, deferring is not a legal request shape — fall back
    # whole rather than send something the API will refuse.
    tools = out.get("tools")
    if isinstance(tools, list) and tools and _deferred_count(tools) >= len(tools):
        return _disable(api_params)

    if not any(_addition_names(m) for m in messages):
        # Nothing to surface yet. A deferred tool still needs the beta header.
        return with_beta(out, DEFERRED_TOOLS_BETA) if _deferred_count(tools) else out

    kept = _place_additions(messages)
    if kept is None:
        return _disable(api_params)
    out["messages"] = kept
    return with_beta(out, DEFERRED_TOOLS_BETA)


def _place_additions(messages: List[Any]) -> Optional[List[Any]]:
    """The rows with each envelope replaced by a ``tool_addition`` system message.

    The position is the first valid one after the envelope: after the user rows that follow
    it and before the next assistant row, or at the end while no assistant row has come. It
    is a function of the history, so a later step sends the message at the same place: it
    stays before the first use of its tool and never moves with the tail. Envelopes before
    one position share one message, because a system message must follow a user message.
    None when no valid position exists.
    """
    kept: List[Any] = []
    pending: List[str] = []

    def flush() -> bool:
        if pending:
            if not kept or not (isinstance(kept[-1], dict) and kept[-1].get("role") == "user"):
                return False
            kept.append(_addition_message(pending))
            pending.clear()
        return True

    for message in messages:
        names = _addition_names(message)
        if names:
            pending.extend(n for n in names if n not in pending)
            continue
        if isinstance(message, dict) and message.get("role") == "assistant" and not flush():
            return None
        kept.append(message)
    return kept if flush() else None


# ---------------------------------------------------------------------------
# the client hooks (kept here so anthropic_client.py does not grow)
# ---------------------------------------------------------------------------

def retry_params_without_deferral(
    client: Any, exc: Any, api_params: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """The un-deferred rebuild to retry with, or None to re-raise *exc* as-is.

    Returns None unless the error IS a deferral refusal AND this request
    actually carried deferral — so an endpoint that 400s for any other reason
    keeps its normal behaviour.
    """
    if not is_deferred_tools_error(exc):
        return None
    carried = bool(_deferred_count(api_params.get("tools")))
    if not carried and isinstance(api_params.get("messages"), list):
        carried = any(_is_addition_system(m) or _addition_names(m)
                      for m in api_params["messages"])
    if not carried:
        return None
    stamp_deferred_unavailable(client)
    logger.warning(
        "Anthropic refused the mid-conversation-tool-changes beta (%s); retrying "
        "this request without deferred tools and not offering them again on this "
        "client: %s", DEFERRED_TOOLS_BETA, exc)
    return prepare_deferred_tools(api_params, enabled=False)


def _self_heal(client: Any, exc: Any, api_params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The rebuilt request after a 400 that names a beta shape, or None to re-raise."""
    return (retry_params_without_deferral(client, exc, api_params)
            or retry_params_without_thinking_controls(client, exc, api_params))


async def call_with_deferral_retry(client: Any, api_params: Dict[str, Any],
                                   use_streaming: bool) -> Any:
    """Issue the tool-path request, self-healing once per beta shape a 400 names."""
    from modules.llm.prefix_stamp import stamp_request

    async def _once(params):
        if use_streaming:
            async with inference_sdk(client).messages.stream(**params) as stream:
                return await stream.get_final_message()
        return await inference_sdk(client).messages.create(**params)

    for attempt in range(_MAX_SELF_HEALS):
        try:
            return await _once(api_params)
        except Exception as exc:
            retry = _self_heal(client, exc, api_params)
            if retry is None:
                raise
            api_params = retry
            stamp_request(api_params)
    return await _once(api_params)


async def stream_with_deferral_retry(client: Any, api_params: Dict[str, Any]):
    """Yield ``("text", chunk)`` then ``("final", message)``, self-healing like the batch call.

    The retry is only offered before the first token reaches the caller — once
    text has been yielded the turn is underway and a silent re-request would
    duplicate it.
    """
    from modules.llm.prefix_stamp import stamp_request

    stamp_request(api_params)
    emitted = False
    attempt = 0
    while True:
        try:
            async with inference_sdk(client).messages.stream(**api_params) as stream:
                async for event in stream:
                    if getattr(event, "type", "") == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is not None and getattr(delta, "type", "") == "text_delta":
                            text = getattr(delta, "text", None)
                            if text:
                                emitted = True
                                yield ("text", text)
                yield ("final", await stream.get_final_message())
            return
        except Exception as exc:
            retry = None if (emitted or attempt >= _MAX_SELF_HEALS) else _self_heal(
                client, exc, api_params)
            if retry is None:
                raise
            attempt += 1
            api_params = retry
            stamp_request(api_params)


__all__ = [
    "LATE_TOOL_MODE_GROW",
    "LATE_TOOL_MODE_BRIDGE",
    "LATE_TOOL_MODE_DEFERRED",
    "DEFERRED_TOOLS_BETA",
    "DEFER_KEY",
    "ADDITION_OPEN",
    "ADDITION_CLOSE",
    "late_tool_mode",
    "model_supports_deferred_tools",
    "deferred_tools_enabled",
    "deferred_tools_unavailable",
    "stamp_deferred_unavailable",
    "is_deferred_tools_error",
    "prepare_deferred_tools",
    "retry_params_without_deferral",
    "call_with_deferral_retry",
    "stream_with_deferral_retry",
]
