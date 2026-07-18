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


def extract_stable_request_id(llm: Any, response: Any, provider: str) -> Optional[str]:
    """Best-effort STABLE idempotency key for ONE LLM completion (G-26
    reachability fix, Task 5c; hardened against cross-completion contention
    in fix pass 2). Two independent billing attempts for the SAME provider
    completion must extract the SAME key so
    ``usage_tracker.record_llm_usage``'s ``request_id`` dedup (partial unique
    index + INSERT OR IGNORE) can actually fire; two DIFFERENT completions
    must never collide onto the same key.

    MOST-PREFERRED source (fix pass 2): the per-call ``_polyrob_provider_response_id``
    attribute stamped directly onto ``response`` by
    ``modules.llm.adapters.LLMClientAdapter._agenerate`` at the point it
    captures the raw provider response, for both the native tool-calling and
    plain generation paths. This is genuinely per-call state -- each
    completion gets its own ``AIMessage`` instance -- so it is immune to the
    concurrency hazard below and is checked FIRST.

    Concurrency hazard this closes: under default-on parallel sub-agent
    delegation (``SUB_AGENTS_ENABLED`` default True, ``MAX_CONCURRENT_SUB_AGENTS=3``),
    ``SubAgentManager.run_subtask`` inherits ``parent_agent.llm`` VERBATIM
    when a subtask has no own model, so multiple concurrent ``Agent.run()``
    loops can share the SAME underlying LLM client object. Reading
    ``<llm>._client.last_response`` (the old sole source, kept below as a
    fallback) is then racy: it is a single mutable slot every call on that
    shared client overwrites, so a caller billing completion A's response
    could read completion B's id if B's call landed on the shared slot
    between A's LLM call returning and A's extraction running -- silently
    dropping (INSERT OR IGNORE) A's real, billed usage_records row
    (under-billing). Because the per-call attribute travels on each
    completion's own response object, this can no longer happen even though
    the client is still shared.

    FALLBACK sources (unchanged from pass 1, kept for responses that predate
    the stamp -- e.g. hand-built test doubles, or a provider whose adapter
    path doesn't reach the stamp site): the provider's own response id
    (Anthropic ``msg_...``, OpenAI/OpenRouter ``chatcmpl-...``, DeepSeek's
    OpenAI-compatible ``id`` field) read directly off ``response.id`` (in
    case it's set there), the ``{'raw': ...}`` structured-output dict shape,
    and finally ``<client>.last_response`` -- every concrete LLM client
    (Anthropic/OpenAI/DeepSeek/OpenRouter) stashes the raw SDK response
    there on EVERY call, including the tool-calling path. Some providers'
    raw response is a dict (DeepSeek's parsed JSON) rather than an SDK
    object, so both shapes are checked. Not every provider's raw response
    carries an id at all (Gemini, and NIM's ``last_response`` is never
    actually populated) -- for those this honestly returns None rather than
    fabricate one.

    Concurrency note (fallback path only): callers MUST invoke this
    synchronously, with no intervening ``await``, right after the
    ``llm.ainvoke(...)`` call that produced ``response`` -- otherwise a later
    concurrent call sharing the same client could have already overwritten
    ``last_response`` before this reads it. The two next_action_internal.py
    billing sites and three of ``meter_aux_llm``'s four callers
    (output_validation.py, compactor.py, completion_judge.py) satisfy this by
    construction. The fourth (``modules/memory/task/reflection_service.py``,
    which defers metering onto a worker thread via
    ``run_coroutine_threadsafe``) does not strictly guarantee it -- but
    ``meter_aux_llm`` is already fully fail-open, and a stale-but-real id
    there costs at worst a rare false "duplicate ignored" ledger-row skip on
    cheap internal maintenance calls, never a wrong charge. The MOST-PREFERRED
    per-call-attribute path above is NOT subject to this caveat.

    Namespaced ``resp:{provider}:{id}`` so a real provider id can never
    collide with the ``_generate_request_id()`` uuid.hex fallback
    ``record_llm_usage`` uses when this returns None, and provider-id
    collisions across two different providers are avoided too. Returns None
    (never fabricates a synthesized key) when no stable id is found -- the
    caller then falls back to record_llm_usage's fresh-uuid legacy behavior,
    so a genuinely-distinct completion is never falsely deduped.
    """
    # MOST-PREFERRED: the per-call attribute (see docstring). Checked before
    # anything shared-client-derived so a concurrent completion sharing the
    # same LLM client can never leak its id onto this one.
    stamped = getattr(response, "_polyrob_provider_response_id", None)
    if isinstance(stamped, str) and stamped:
        return f"resp:{provider or 'unknown'}:{stamped}"

    candidates = []
    if isinstance(response, dict):
        candidates.append(response.get("raw"))
    candidates.append(response)
    client = getattr(llm, "_client", None)
    candidates.append(getattr(client, "last_response", None))

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
        request_id = extract_stable_request_id(llm, response, provider)
        await usage_tracker.record_llm_usage(
            user_id=user_id, session_id=session_id, agent_id=agent_id,
            model=model, provider=provider,
            input_tokens=usage.get("prompt_tokens") or 0,
            output_tokens=usage.get("completion_tokens") or 0,
            cached_tokens=usage.get("cached_tokens") or 0,
            duration_seconds=duration_seconds, component=component, purpose=purpose, success=True,
            request_id=request_id,
        )
    except Exception as e:  # fail-open (incl. InsufficientCreditsError): never break the aux op
        logger.warning(f"aux metering skipped ({component}/{purpose}): {type(e).__name__}: {e}")
