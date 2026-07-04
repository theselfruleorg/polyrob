"""ReflectionService — extracted LLM-consolidation concern (Part A SoC refactor).

Previously inlined as TaskContextManager._llm_consolidate.  Owns the single
responsibility of synthesising a phase summary from a list of findings via an
aux model (H-MEM §3.3), with fail-open fallback to None on any error so the
caller can substitute a simple concat.

Gate: ``REFLECTION_LLM_ENABLED`` — read once at construction time via the
single-source helper ``agents.task.constants.reflection_llm_enabled_default()``.
The aux LLM is provisioned externally (by ``construction.py``) and injected as
the ``llm`` parameter; when ``llm is None`` the service returns None immediately
(fail-open, same as disabled).

Behaviour contract (matches the historical _llm_consolidate exactly):
- Returns the LLM-synthesized text when enabled + model available + findings non-empty.
- Returns None when disabled / no model / empty findings / empty LLM response.
- Returns None on ANY exception from the LLM call (fail-open).
- Emits ``event=reflection_consolidate`` (INFO) on success.
- Emits ``event=reflection_fallback`` (INFO on empty response, WARNING on error).
- Uses ``core.async_bridge.run_coroutine_sync`` for the sync-over-async call,
  bounded to 30 seconds (mirrors the removed inlining in TCM, P4 pattern).
"""
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


class ReflectionService:
    """Synthesises a phase summary from findings via an aux LLM.

    Args:
        enabled: Whether LLM consolidation is active.  Typically set from
            ``constants.reflection_llm_enabled_default()``.
        llm: Provisioned aux model (any object with an ``ainvoke`` coroutine
            that accepts a list of messages and returns an object with a
            ``content`` attribute).  Pass ``None`` to run in concat-only mode.
        meter_ctx: Optional dict with ``usage_tracker``/``user_id``/``session_id``/
            ``agent_id`` (A3) — plus an optional ``loop`` (the main event loop
            captured at construction) — used to bill this aux LLM call through the
            single deduction path. Because consolidation runs on a worker thread,
            the meter is scheduled back onto ``loop`` (loop-affinity fix); absent
            ``loop`` falls back to the legacy ``run_coroutine_sync`` path.
            Absent/empty => no metering (byte-identical to before A3), matching the
            fail-open contract of ``meter_aux_llm`` itself.
    """

    def __init__(self, enabled: bool, llm: Optional[object], meter_ctx: Optional[dict] = None) -> None:
        self._enabled = enabled
        self._llm = llm
        self._meter_ctx = meter_ctx or {}

    def _meter(self, mc: dict, resp: object, duration_seconds: float) -> None:
        """Bill the aux reflection call, scheduling onto the ORIGINAL loop.

        Reflection consolidation runs on a worker thread (add_step_memory is
        offloaded via ``asyncio.to_thread``). ``run_coroutine_sync`` there spins a
        THROWAWAY event loop, but the usage tracker's DB connection binds an
        ``asyncio.Lock`` to the MAIN loop — so metering on the throwaway loop
        raises "bound to a different event loop", which ``meter_aux_llm`` swallows
        (fail-open) → reflection is never billed. When construction captured the
        main loop (``mc["loop"]``), schedule the meter back onto it via
        ``run_coroutine_threadsafe`` (the main loop is free — only the worker
        thread blocks on ``.result()`` — so there is no deadlock). Falls back to
        the legacy ``run_coroutine_sync`` when no loop was captured. Fully
        fail-open: any scheduling error is logged and swallowed so the reflection
        summary is preserved.
        """
        try:
            import asyncio
            from agents.task.agent.core.aux_metering import meter_aux_llm
            coro = meter_aux_llm(
                usage_tracker=mc["usage_tracker"], user_id=mc["user_id"],
                session_id=mc.get("session_id", ""), agent_id=mc.get("agent_id", "") or "",
                llm=self._llm, response=resp, duration_seconds=duration_seconds,
                component="reflection", purpose="hmem_consolidate",
            )
            loop = mc.get("loop")
            if loop is not None and not loop.is_closed():
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                fut.result(timeout=30.0)
            else:
                from core.async_bridge import run_coroutine_sync
                run_coroutine_sync(coro, timeout=30.0)
        except Exception as e:
            logger.warning(f"event=reflection_meter_failed detail={e}")

    def consolidate(self, findings: List[str]) -> Optional[str]:
        """Return an LLM-synthesised summary of *findings*, or None.

        None signals the caller to fall back to ``"; ".join(findings)`` concat.

        Args:
            findings: List of finding strings for this phase.

        Returns:
            Synthesised summary string, or None on any failure / when disabled.
        """
        if not (self._enabled and self._llm and findings):
            return None

        try:
            import time
            from core.async_bridge import run_coroutine_sync
            from modules.llm.messages import HumanMessage

            prompt = (
                "Consolidate these related findings into a concise phase summary "
                "(<=3 sentences). Preserve concrete facts, names, and numbers; drop "
                "redundancy:\n\n" + "\n".join(f"- {f}" for f in findings)
            )
            _t0 = time.perf_counter()
            resp = run_coroutine_sync(
                self._llm.ainvoke([HumanMessage(content=prompt)]),
                timeout=30.0,
            )
            _duration = time.perf_counter() - _t0

            # A3: meter this aux LLM call through the single deduction path.
            # Isolated in its own try/except so a metering failure NEVER drops the
            # (already computed) reflection summary below.
            mc = self._meter_ctx
            if mc.get("usage_tracker") and mc.get("user_id"):
                self._meter(mc, resp, _duration)

            text = (getattr(resp, "content", None) or "").strip()
            if text:
                logger.info(
                    f"event=reflection_consolidate findings={len(findings)} chars={len(text)}"
                )
                return text
            logger.info("event=reflection_fallback reason=empty_response")
            return None
        except Exception as e:
            logger.warning(
                f"event=reflection_fallback reason=error detail={e} (using concat)"
            )
            return None
