"""The result of one LLM call: what the provider's response says, on the call's own message.

A client leaves its raw response in one slot that sessions share. The adapter is the one
reader: it reads the slot in the tick the client returned, with no await since, builds a
:class:`CallFacts` value and puts it on that call's ``AIMessage``. Every consumer reads
``message.call_facts``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from core.stop_reason import normalize_stop
from modules.llm.call_scope import CallRequest


@dataclass(frozen=True)
class CallFacts:
    """What one finished call was, for the usage record and the step that made it."""
    billed_source: Optional[str] = None   # "provider" | "estimate"
    finish_reason: Optional[str] = None
    upstream: Optional[str] = None
    reasoning_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    request_stamps: Dict[str, str] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)

    @property
    def stop(self) -> Optional[str]:
        return normalize_stop(self.finish_reason)

    def stamps(self, include_context: bool = True) -> Dict[str, Any]:
        """The usage-record metadata of this call. An aux caller omits the step's context."""
        out: Dict[str, Any] = dict(self.request_stamps)
        for key in ("upstream", "reasoning_tokens", "finish_reason"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.billed_source:
            out["billed_source"] = self.billed_source
        if include_context:
            out.update(self.context)
        return out


def _field(obj: Any, name: str) -> Any:
    """`obj[name]` for a dict, else the attribute, else the pydantic extra."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    value = getattr(obj, name, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get(name)
    return value


def response_stamps(response: Any) -> Dict[str, Any]:
    """What the provider's response says about the call.

    * ``upstream``: the OpenRouter field ``provider``, the upstream that served the call.
    * ``reasoning_tokens``: ``usage.completion_tokens_details.reasoning_tokens`` (Chat
      Completions) or ``usage.output_tokens_details.reasoning_tokens`` (Responses).

    A key is absent when the response does not carry it. Never raises.
    """
    out: Dict[str, Any] = {}
    try:
        upstream = _field(response, "provider")
        if isinstance(upstream, str) and upstream.strip():
            out["upstream"] = upstream.strip()[:64]
        usage = _field(response, "usage")
        for details_key in ("completion_tokens_details", "output_tokens_details"):
            reasoning = _field(_field(usage, details_key), "reasoning_tokens")
            if isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning > 0:
                out["reasoning_tokens"] = reasoning
                break
    except Exception:
        return out
    return out


def _first_choice(response: Any) -> Any:
    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    if not choices:
        return None
    try:
        return choices[0]
    except (IndexError, TypeError, KeyError):
        return None


def finish_reason_of(response: Any) -> Optional[str]:
    """The provider's stop reason in this raw response, or None.

    None means "could not tell", never "it finished cleanly". Every caller must treat it as
    unknown rather than as a negative.
    """
    if response is None:
        return None
    # Anthropic: a top-level stop_reason, no choices.
    stop = getattr(response, "stop_reason", None)
    if isinstance(stop, str) and stop:
        return stop
    # Responses API: ``status: incomplete`` with the reason beside it.
    incomplete = getattr(getattr(response, "incomplete_details", None), "reason", None)
    if isinstance(incomplete, str) and incomplete:
        return incomplete
    choice = _first_choice(response)
    if choice is None:
        return _gemini_finish_reason(response)
    reason = getattr(choice, "finish_reason", None)
    if reason is None and isinstance(choice, dict):
        reason = choice.get("finish_reason") or choice.get("native_finish_reason")
    return reason if isinstance(reason, str) and reason else None


def _gemini_finish_reason(response: Any) -> Optional[str]:
    """Gemini: ``candidates[0].finish_reason``, a proto enum."""
    candidates = getattr(response, "candidates", None)
    if not candidates or not hasattr(candidates, "__getitem__"):
        return None
    from modules.llm.gemini_parts import enum_name
    return enum_name(getattr(candidates[0], "finish_reason", None))


def call_facts(response: Any, usage: Optional[Dict[str, Any]],
               request: Optional[CallRequest]) -> CallFacts:
    """The facts of one finished call. *response* is the provider's raw response of THIS
    call, read in the tick the client returned; *usage* is its ``usage_metadata``."""
    usage = usage or {}
    read = response_stamps(response)
    billed = usage.get("billed_cost_usd") is not None
    return CallFacts(
        billed_source=(usage.get("billed_source") or "provider") if billed else None,
        finish_reason=finish_reason_of(response),
        upstream=read.get("upstream"),
        reasoning_tokens=read.get("reasoning_tokens"),
        output_tokens=usage.get("output_tokens"),
        request_stamps=dict(request.stamps) if request else {},
        context=dict(request.context) if request else {},
    )


def read_stamps(response: Any, include_context: bool = True) -> Dict[str, Any]:
    """The usage-record stamps of *response*, the message the call returned. Empty when
    the message carries no facts (a provider path with no adapter)."""
    facts = getattr(response, "call_facts", None)
    return facts.stamps(include_context) if isinstance(facts, CallFacts) else {}


def output_was_truncated(facts: Optional[CallFacts]) -> bool:
    """True only when the provider SAID that this call ran out of output budget."""
    return facts is not None and facts.stop == "length"


#: The seat whose reasoning text goes back on a later request: only native DeepSeek, whose
#: API requires it (``adapters.py``). Text with another tag, or none, never goes back.
REASONING_SEAT_KEY = "reasoning_seat"
NATIVE_DEEPSEEK_SEAT = "deepseek-native"

#: What an assistant message keeps through the history and the session files for a later
#: request: the reasoning text with the seat that wrote it, and Anthropic's signed blocks
#: under their own key, which only the Anthropic adapter reads.
_REPLAY_KEYS = ("reasoning_content", REASONING_SEAT_KEY, "anthropic_thinking")


def replay_fields(source: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The replay keys that *source* holds with a value."""
    return {key: source[key] for key in _REPLAY_KEYS if source and source.get(key)}


def replayed_reasoning(message: Any) -> str:
    """The reasoning text a stored message sends again on a later request: only text that
    native DeepSeek wrote. Old untagged text stays in the history and is never sent."""
    stored = getattr(message, "additional_kwargs", None) or {}
    value = stored.get("reasoning_content")
    if stored.get(REASONING_SEAT_KEY) != NATIVE_DEEPSEEK_SEAT:
        return ""
    return value if isinstance(value, str) else ""


#: A previous call that read the cache and a call that lost over half of it: the event a
#: usage record names a cause for.
_HIT_SHARE = 0.5
_LOST_SHARE = 0.5
_REWRITE_SHARE = 0.8


def _moved(prev: Dict[str, Any], cur: Dict[str, Any], key: str) -> bool:
    return bool(prev.get(key)) and bool(cur.get(key)) and prev[key] != cur[key]


def cold_call_cause(prev: Optional[Dict[str, Any]], cur: Dict[str, Any]) -> Optional[str]:
    """Why *cur* went cold after *prev* in one session, or None when it did not. Each dict
    holds ``input_tokens``, ``cached_tokens``, ``model`` and the call's stamps."""
    if not prev:
        return None
    prev_cached = prev.get("cached_tokens") or 0
    if prev_cached <= _HIT_SHARE * (prev.get("input_tokens") or 0) or not prev_cached:
        return None
    if (cur.get("cached_tokens") or 0) >= _LOST_SHARE * prev_cached:
        return None
    if _moved(prev, cur, "upstream") or _moved(prev, cur, "model"):
        return "upstream_moved"
    if _moved(prev, cur, "tools_sha"):
        return "tools_changed"
    if _moved(prev, cur, "foundation_sha") or _moved(prev, cur, "prefix_sha"):
        return "foundation_changed"
    if (cur.get("input_tokens") or 0) < _REWRITE_SHARE * (prev.get("input_tokens") or 0):
        return "compaction"
    return "expired_or_unknown"
