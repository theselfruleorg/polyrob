"""Pre-LLM token budget: prune once before the overflow kills the run.

The step-boundary compaction gate (``core/step.py``) reads the running
estimate; token counts are RECALIBRATED just before the LLM call, and a step
that appended several large tool results can cross the safe line only after
that recalibration — the gate never saw it. Raising there ends the run with
no fallback provider (prod 2026-09-18 14:34Z, ``966d3539``). The emergency
prune is the non-LLM safety net designed for exactly this; give it one turn
before raising, and raise only if the history is still over the line.
"""
from __future__ import annotations

import logging
from typing import Any, Dict


def ensure_token_budget(message_manager: Any, logger: logging.Logger) -> Dict[str, Any]:
    """Return the safety-check dict; prune once on overflow; raise if still unsafe."""
    check = message_manager.check_token_safety(raise_on_overflow=False)
    if check.get("safe", True):
        return check
    logger.warning(
        "🚨 Pre-call token overflow (%s/%s, %.1f%%) — emergency prune before raising",
        check.get("current_tokens"), check.get("max_limit"), check.get("usage_percent", 0.0))
    try:
        message_manager.emergency_context_prune()
    except Exception:
        logger.warning("emergency prune failed; re-checking budget", exc_info=True)
    return message_manager.check_token_safety(raise_on_overflow=True)
