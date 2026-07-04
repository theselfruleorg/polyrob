"""Meter auxiliary LLM calls (compaction, judge, reflection) through the single
usage/deduction path. These calls were unbilled — only the main step
(next_action_internal.py:388) recorded usage — so long-context compaction on the
flagship model was free. Per operator decision aux calls DEDUCT like main calls, but
this helper FAILS OPEN on any error (including InsufficientCreditsError): aux calls are
internal maintenance and must never hard-halt a session. The deduction still happens
whenever the balance covers it.
"""
from __future__ import annotations

from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)


def _llm_identity(llm: Any, response: Any = None) -> tuple[str, str]:
    """Derive (model, provider) from the llm object, falling back to detecting
    the provider from the model name via ``detect_llm_provider`` (which takes a
    RESPONSE + optional model name — the response param is unused by that
    function but kept for signature compatibility; the model name is what it
    actually looks up in the model registry).
    """
    model = (getattr(llm, "model_name", None) or getattr(llm, "model", None)
             or getattr(llm, "model_type", None) or "unknown")
    provider = (getattr(llm, "llm_provider", None) or getattr(llm, "provider_name", None))
    if not provider:
        try:
            from agents.task.utils import detect_llm_provider
            provider = detect_llm_provider(response, model) or "unknown"
        except Exception:
            provider = "unknown"
    return str(model), str(provider)


async def meter_aux_llm(*, usage_tracker, user_id: Optional[str], session_id: str,
                        agent_id: str, llm: Any, response: Any, duration_seconds: float,
                        component: str, purpose: str) -> None:
    """Record usage for an auxiliary (non-main-step) LLM call.

    Fail-open: no-ops silently when there's no tracker/user_id (e.g. anonymous
    local sessions), and swallows ANY exception from token extraction or
    ``record_llm_usage`` itself (including InsufficientCreditsError) — an aux
    call is internal maintenance and must never abort the caller.
    """
    if not (usage_tracker and user_id):
        return
    try:
        from agents.task.utils import extract_token_usage
        model, provider = _llm_identity(llm, response)
        usage = extract_token_usage(response, provider) or {}
        await usage_tracker.record_llm_usage(
            user_id=user_id, session_id=session_id, agent_id=agent_id,
            model=model, provider=provider,
            input_tokens=usage.get("prompt_tokens") or 0,
            output_tokens=usage.get("completion_tokens") or 0,
            cached_tokens=usage.get("cached_tokens") or 0,
            duration_seconds=duration_seconds, component=component, purpose=purpose, success=True,
        )
    except Exception as e:  # fail-open (incl. InsufficientCreditsError): never break the aux op
        logger.warning(f"aux metering skipped ({component}/{purpose}): {type(e).__name__}: {e}")
