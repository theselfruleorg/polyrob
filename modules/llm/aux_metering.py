"""Meter auxiliary LLM calls (compaction, judge, reflection) through the single
usage/deduction path. These calls were unbilled — only the main step
(next_action_internal.py:388) recorded usage — so long-context compaction on the
flagship model was free. Per operator decision aux calls DEDUCT like main calls, but
this helper FAILS OPEN on any error (including InsufficientCreditsError): aux calls are
internal maintenance and must never hard-halt a session. The deduction still happens
whenever the balance covers it.

Core-tier-free helper (stdlib only). Lives in the LLM layer, not agents/task/agent/core/: modules/llm/messages, modules/credits/usage_tracker and
modules/memory/task/reflection_service would otherwise import it upward.
"""
from __future__ import annotations

from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)


def extract_stable_request_id(response: Any, provider: str) -> Optional[str]:
    """Best-effort STABLE idempotency key for ONE LLM completion.
    Two independent billing attempts for the SAME provider
    completion must extract the SAME key so
    ``usage_tracker.record_llm_usage``'s ``request_id`` dedup (partial unique
    index + INSERT OR IGNORE) can actually fire; two DIFFERENT completions
    must never collide onto the same key.

    Sources: the per-call ``_polyrob_provider_response_id`` the adapter puts on this
    completion's own ``AIMessage``, then the id on ``response`` itself or its
    ``{'raw': ...}`` dict. A client is shared by parallel sub-agents, so its
    ``last_response`` can hold another completion's id: reading it could drop THIS
    billed row as a duplicate. Not every raw response carries an id (Gemini): this
    then returns None and never fabricates one.

    Namespaced ``resp:{provider}:{id}`` so a real provider id can never
    collide with the ``_generate_request_id()`` uuid.hex fallback
    ``record_llm_usage`` uses when this returns None, and provider-id
    collisions across two different providers are avoided too. Returns None
    (never fabricates a synthesized key) when no stable id is found -- the
    caller then falls back to record_llm_usage's fresh-uuid legacy behavior,
    so a genuinely-distinct completion is never falsely deduped.
    """
    # A reserved call carries its own durable reference through every metering
    # path, including streaming and auxiliary calls. Never use shared client state.
    reserved = getattr(response, "_polyrob_billing_reservation_id", None)
    if isinstance(response, dict):
        reserved = reserved or getattr(response.get("raw"), "_polyrob_billing_reservation_id", None)
    if isinstance(reserved, str) and reserved.startswith("llm-reserve:"):
        from core.billing_context import hold_handed_off
        hold_handed_off(reserved)   # the metering path owns it now
        return reserved
    stamped = getattr(response, "_polyrob_provider_response_id", None)
    if isinstance(stamped, str) and stamped:
        return f"resp:{provider or 'unknown'}:{stamped}"

    candidates = []
    if isinstance(response, dict):
        candidates.append(response.get("raw"))
    candidates.append(response)

    for cand in candidates:
        if cand is None:
            continue
        rid = cand.get("id") if isinstance(cand, dict) else getattr(cand, "id", None)
        if isinstance(rid, str) and rid:
            return f"resp:{provider or 'unknown'}:{rid}"
    return None


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
            # A chat-model ADAPTER carries neither attribute above, so this is the
            # live path for every aux call. Ask the client which ProviderSpec built
            # it before falling back to model-name detection: the registry credits
            # a model id to the vendor that ORIGINATED it, so a judge running on a
            # zai-coding seat was recorded as `openrouter`/`glm-4.6` — an
            # impossible pair — and billed a flat-rate seat as metered.
            from modules.llm.usage_extract import resolve_serving_provider
            provider = resolve_serving_provider(llm, model) or "unknown"
        except Exception:
            provider = "unknown"
    return str(model), str(provider)


def _aux_stamps(response: Any) -> Optional[dict]:
    """The call's stamps for an aux usage row; None when there are none."""
    from modules.llm.call_result import read_stamps
    return read_stamps(response, include_context=False) or None


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
        from modules.llm.usage_extract import extract_token_usage
        model, provider = _llm_identity(llm, response)
        usage = extract_token_usage(response) or {}
        request_id = extract_stable_request_id(response, provider)
        await usage_tracker.record_llm_usage(
            user_id=user_id, session_id=session_id, agent_id=agent_id,
            model=model, provider=provider,
            input_tokens=usage.get("prompt_tokens") or 0,
            output_tokens=usage.get("completion_tokens") or 0,
            cached_tokens=usage.get("cached_tokens") or 0,
            # An aux call's cache write is billed (calculate_cost sees it), so it is recorded.
            cache_creation_tokens=usage.get("cache_creation_tokens") or 0,
            cache_creation_1h_tokens=usage.get("cache_creation_1h_tokens") or 0,
            duration_seconds=duration_seconds, component=component, purpose=purpose, success=True,
            request_id=request_id,
            # What the provider BILLED outranks the catalog estimate on the aux path too.
            billed_cost_usd=usage.get("billed_cost_usd"),
            cache_discount_usd=usage.get("cache_discount_usd"),
            # The same per-call stamps the main step records (prefix,
            # upstream, reasoning tokens) — never the agent step's context slots.
            metadata=_aux_stamps(response),
        )
    except Exception as e:  # fail-open (incl. InsufficientCreditsError): never break the aux op
        logger.warning(f"aux metering skipped ({component}/{purpose}): {type(e).__name__}: {e}")
