"""The inputs of one LLM call, never on the client that sessions share.

The inputs are context variables, so each task reads its own values. The result of a call
is in :mod:`modules.llm.call_result`.
"""
import contextvars
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, ContextManager, Dict, Iterator, Optional


@dataclass(frozen=True)
class _CallScope:
    timeout: Optional[float] = None
    input_tokens: Optional[int] = None
    max_retries: Optional[int] = None


_call_scope: contextvars.ContextVar[_CallScope] = contextvars.ContextVar(
    "llm_call_scope", default=_CallScope())
_cache_ttl: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_cache_ttl", default=None)
_foundation_len: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "llm_foundation_len", default=None)
_provider_sort: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_provider_sort", default=None)
_session_tag: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_session_tag", default=None)
_step_context: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "llm_step_context", default=None)
_session_output_cap: contextvars.ContextVar[int] = contextvars.ContextVar(
    "llm_session_output_cap", default=0)
_call_output_cap: contextvars.ContextVar[int] = contextvars.ContextVar(
    "llm_call_output_cap", default=0)
_reasoning_level: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_reasoning_level", default=None)


#: The values a setter below writes for the calls of one :func:`call_scope` block.
_SCOPED_VALUES = (_cache_ttl, _foundation_len, _provider_sort, _session_tag, _step_context)


@contextmanager
def call_scope(timeout: Optional[float] = None, input_tokens: Optional[int] = None,
               max_retries: Optional[int] = None) -> Iterator[None]:
    """The calls of this block: their time budget, their provider-anchored input count and
    their SDK retry count. A nested block replaces all three, so an aux call inside a step
    does not read the step's anchor. Each value a setter writes inside the block ends with
    the block, by token. ``asyncio.wait_for`` runs its coroutine in the caller's own task on
    Python 3.12, so the end of a task does not end a value."""
    tokens = [(var, var.set(var.get())) for var in _SCOPED_VALUES]
    tokens.append((_call_scope, _call_scope.set(
        _CallScope(timeout=timeout, input_tokens=input_tokens, max_retries=max_retries))))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def anchored_input_tokens() -> Optional[int]:
    return _call_scope.get().input_tokens


def scoped_max_retries() -> Optional[int]:
    """The SDK retry count of this block's calls. None keeps the client's own count."""
    return _call_scope.get().max_retries


def call_timeout_param() -> Dict[str, float]:
    """The per-request ``timeout`` of a scoped call. The caller's ``wait_for`` owns that
    budget, so the SDK limit must not cut the call sooner. Empty outside a scope."""
    timeout = _call_scope.get().timeout
    return {"timeout": float(timeout)} if timeout else {}


def set_cache_ttl(ttl: Optional[str]) -> None:
    _cache_ttl.set(ttl)


def scoped_cache_ttl() -> Optional[str]:
    return _cache_ttl.get()


def set_foundation_len(n: Optional[int]) -> None:
    """The count of leading non-system wire rows that are the pinned foundation."""
    valid = isinstance(n, int) and not isinstance(n, bool) and n >= 1
    _foundation_len.set(n if valid else None)


def scoped_foundation_len() -> Optional[int]:
    return _foundation_len.get()


def set_scoped_provider_sort(sort: Optional[str]) -> None:
    _provider_sort.set(sort)


def scoped_provider_sort() -> Optional[str]:
    return _provider_sort.get()


def set_session_tag(session_id: Optional[str]) -> None:
    """A digest of the session id for a provider's session field. The raw id stays here."""
    text = str(session_id) if session_id else ""
    _session_tag.set(hashlib.sha256(text.encode("utf-8")).hexdigest()[:32] if text else None)


def session_tag() -> Optional[str]:
    return _session_tag.get()


def set_step_context(stamps: Optional[Dict[str, Any]]) -> None:
    """The context reading of the agent step that makes the calls of this block."""
    _step_context.set(stamps or None)


def set_session_output_cap(cap: int) -> int:
    """This task's session output cap, set once a step for every session class. Returns the
    cap in force, 0 = none. An asyncio task copies its parent's context, so 0 clears what a
    parent run carried in."""
    value = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else 0
    _session_output_cap.set(value)
    return value


def session_output_cap() -> int:
    return _session_output_cap.get()


@contextmanager
def _holding(var: contextvars.ContextVar, value: Any) -> Iterator[None]:
    token = var.set(value)
    try:
        yield
    finally:
        var.reset(token)


def bounded_output(cap: int) -> ContextManager[None]:
    """Narrow the output ceiling of the calls inside to *cap*, for this task only."""
    return _holding(_call_output_cap, cap if isinstance(cap, int) and cap > 0 else 0)


def call_output_cap() -> int:
    return _call_output_cap.get()


def session_reasoning(level: Optional[str]) -> ContextManager[None]:
    """Apply the owner's reasoning *level* (``off|on|low|medium|high|<tokens>``) to the
    calls inside: one session's main-model calls on a client that sessions share."""
    return _holding(_reasoning_level, level or None)


def session_reasoning_level() -> Optional[str]:
    return _reasoning_level.get()


@dataclass
class CallRequest:
    """The request half of one call, open from :func:`begin_call` to the result."""
    stamps: Dict[str, str] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)


_request: contextvars.ContextVar[Optional[CallRequest]] = contextvars.ContextVar(
    "llm_call_request", default=None)


def begin_call() -> CallRequest:
    """Open this task's call. A client that sends a request writes its stamps here."""
    request = CallRequest(context=dict(_step_context.get() or {}))
    _request.set(request)
    return request


def note_request_stamps(stamps: Dict[str, str]) -> None:
    """The stamps of the request on the wire. A retry inside the client overwrites them."""
    request = _request.get()
    if request is not None:
        request.stamps = dict(stamps)



