"""Per-session OUTPUT budget, and 'was the last answer cut off?' — 057 WS-B.

**Latency is output-bound.** The 24 h prod measurement (2026-09-20): ≤1k output
tokens → 13 s, 5k → 101 s, 16k → 272 s, while UNCACHED INPUT was flat at 41-63 s
across every bucket. Five calls a day hit the 16,384-token wire ceiling and ran
172-330 s each — against a 600 s cron cap.

Two things live here, both keyed off the same fact:

1. :func:`output_token_cap` — the ONE answer to "how many output tokens may this
   request ask for". It is ``LLM_MAX_OUTPUT_TOKENS`` (the existing absolute
   ceiling: cost + the credit pre-authorisation a metered provider performs),
   narrowed by ``AUTONOMOUS_MAX_OUTPUT_TOKENS`` for a cron/goal session that has
   been stamped by :func:`apply_session_output_budget`. An owner's chat keeps the
   old ceiling.
2. :func:`finish_reason_of` / :func:`output_was_truncated` — whether the provider
   stopped because it ran out of budget. Nothing in the tree surfaced this
   before, so a cut-off answer arrived as malformed JSON and was "repaired" into
   a guess. A budget with no truncation signal is worse than no budget.

⚠️ The truncation probe reads the client's ``last_response``, which every client
already stores for usage extraction — deliberately, so no line changes in
``llm_client.py`` / ``openrouter_client.py`` / ``adapters.py`` (all at their size
ceilings). Every shape is handled (OpenAI-style object, DeepSeek's dict,
Anthropic's ``stop_reason``); an unreadable response is ``None``, never a
confident "not truncated".

Defaults: ``AUTONOMOUS_MAX_OUTPUT_TOKENS=0`` = off, byte-identical.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: attribute stamped on a client/adapter by apply_session_output_budget.
_ATTR = "_polyrob_session_output_cap"

#: finish reasons that mean "I ran out of output budget", across providers.
_TRUNCATED = frozenset({"length", "max_tokens", "MAX_TOKENS", "model_length"})


def autonomous_max_output_tokens() -> int:
    """``AUTONOMOUS_MAX_OUTPUT_TOKENS`` — 0 (default) = off."""
    from core.env import int_env
    value = int_env("AUTONOMOUS_MAX_OUTPUT_TOKENS", 0)
    return value if value > 0 else 0


def apply_output_budget(llm: Any, cap: int) -> int:
    """Stamp an output cap onto *llm* (and its wrapped client). Returns the cap.

    Idempotent and cheap — call it once a step. Re-stamping each step is
    deliberate: a provider fallback builds a NEW client mid-run, and a budget
    that silently stopped applying after a fallback would be a budget nobody
    could trust. Fail-open.

    ⚠️ WHICH sessions get a budget is not decided here — ``modules`` may not
    import ``agents`` (layering ratchet). The agents-tier wrapper is
    ``agents.task.session_class.apply_session_output_budget``.
    """
    if llm is None or cap <= 0:
        return 0
    try:
        setattr(llm, _ATTR, cap)
        client = getattr(llm, "_client", None)
        if client is not None:
            setattr(client, _ATTR, cap)
    except Exception:  # pragma: no cover - defensive
        logger.debug("session output budget not applied", exc_info=True)
        return 0
    return cap


def output_token_cap(client: Any, default_cap: int) -> int:
    """The per-request output ceiling: ``LLM_MAX_OUTPUT_TOKENS`` narrowed by this
    session's autonomous budget, if one was stamped."""
    from core.env import int_env
    cap = int_env("LLM_MAX_OUTPUT_TOKENS", default_cap)
    if cap <= 0:
        cap = default_cap
    # Only an int counts. `int(x)` on an arbitrary object is a trap — a MagicMock
    # answers 1, which would silently pin every request to a 1-token ceiling.
    raw = getattr(client, _ATTR, 0)
    session_cap = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
    if session_cap > 0:
        return min(cap, session_cap)
    return cap


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


def finish_reason_of(client: Any) -> Optional[str]:
    """The provider's stop reason for this client's LAST call, or None.

    None means "could not tell" — never "it finished cleanly". Every caller must
    treat it as unknown rather than as a negative.
    """
    response = getattr(client, "last_response", None)
    if response is None:
        return None
    # Anthropic: a top-level stop_reason, no choices.
    stop = getattr(response, "stop_reason", None)
    if isinstance(stop, str) and stop:
        return stop
    choice = _first_choice(response)
    if choice is None:
        return None
    reason = getattr(choice, "finish_reason", None)
    if reason is None and isinstance(choice, dict):
        reason = choice.get("finish_reason") or choice.get("native_finish_reason")
    return reason if isinstance(reason, str) and reason else None


def output_was_truncated(llm: Any) -> bool:
    """True only when the provider SAID it ran out of output budget."""
    client = getattr(llm, "_client", None) or llm
    reason = finish_reason_of(client)
    return bool(reason) and reason in _TRUNCATED


def last_output_tokens(llm: Any) -> Optional[int]:
    """Completion tokens on the last call, for an honest 'cut at N' note."""
    client = getattr(llm, "_client", None) or llm
    response = getattr(client, "last_response", None)
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    for name in ("completion_tokens", "output_tokens"):
        value = getattr(usage, name, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(name)
        if isinstance(value, int) and value > 0:
            return value
    return None


def last_reasoning_tokens(llm: Any) -> Optional[int]:
    """Reasoning (thinking) tokens inside the last call's completion, or None
    when the provider did not report them — never 0 for "not reported".
    OpenRouter/OpenAI shape: ``usage.completion_tokens_details.reasoning_tokens``.
    Prod 2026-09-20: 7,981 of an 8,192-token cut were reasoning — the model was
    cut before its tool call, which is a different fact from a long write."""
    client = getattr(llm, "_client", None) or llm
    response = getattr(client, "last_response", None)
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None
    details = getattr(usage, "completion_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("completion_tokens_details")
    if details is None:
        return None
    value = getattr(details, "reasoning_tokens", None)
    if value is None and isinstance(details, dict):
        value = details.get("reasoning_tokens")
    return value if isinstance(value, int) and value >= 0 else None
