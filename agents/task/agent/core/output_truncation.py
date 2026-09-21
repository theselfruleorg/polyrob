"""A cut-off answer is a FACT, not malformed JSON — 057 WS-B.

Five calls a day hit the 16,384-token output ceiling on prod (172-330 s each,
against a 600 s cron cap). Nothing in the tree read ``finish_reason``, so the
half-written answer arrived as broken JSON and went through the repair path,
which guesses. The model was then told nothing about WHY, so it wrote the same
oversized file again.

This module closes that: when the provider says it ran out of output budget, the
step is re-run ONCE with an injected control note naming the cut and the remedy
("write the file in two calls"), instead of parsing a truncated answer. A
``llm_output_truncated`` telemetry event is emitted either way, so the count is
measurable — that is the acceptance number in 057 WS-B (``finish_reason=length``
= 0/day).

Gates: the retry rides ``LLM_OUTPUT_TRUNCATION_RETRY`` (default OFF =
byte-identical). The telemetry event is NOT gated — it observes, it does not act.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

_NOTE = (
    "Your last response was CUT OFF at {tokens} output tokens — the provider "
    "stopped because the answer reached the output budget, not because it was "
    "finished. Nothing from it was used. Do the SAME step again, smaller: write "
    "a long file in two or more calls (write_file, then append_file), and keep "
    "each tool call's arguments well under the budget. Do not repeat the "
    "oversized call."
)


_REASONING_NOTE = (
    "Your last response was CUT OFF at {tokens} output tokens — and {reasoning} of "
    "them were REASONING: you thought past the output budget and never reached "
    "your tool call. Nothing from it was used. Do the SAME step again: decide in "
    "one or two sentences, then call the tool. Do not re-derive what the context "
    "already shows."
)

# Above this share of the cut, the answer never started — the reasoning is the
# thing to bound, and a "write smaller" note would be the wrong diagnosis.
REASONING_BOUND_SHARE = 0.8


def reasoning_bound(tokens: Optional[int], reasoning_tokens: Optional[int]) -> bool:
    """True when the cut was spent on reasoning, not on the answer. Unknown
    reasoning (provider silent) is NOT reasoning-bound — the write note stays."""
    if not tokens or reasoning_tokens is None:
        return False
    return reasoning_tokens >= REASONING_BOUND_SHARE * tokens


def truncation_retry_enabled() -> bool:
    """``LLM_OUTPUT_TRUNCATION_RETRY`` — default OFF."""
    from core.env import bool_env
    return bool_env("LLM_OUTPUT_TRUNCATION_RETRY", False)


def _emit(agent: Any, tokens: Optional[int], retried: bool,
          reasoning_tokens: Optional[int] = None) -> None:
    try:
        from core.event_log import emit
        emit("llm_output_truncated",
             source="agent.llm_runner",
             user_id=str(getattr(agent, "user_id", "") or ""),
             session_id=str(getattr(agent, "session_id", "") or ""),
             attrs={"output_tokens": tokens,
                    "reasoning_tokens": reasoning_tokens,
                    "reasoning_bound": reasoning_bound(tokens, reasoning_tokens),
                    "model": str(getattr(agent, "model_name", "") or ""),
                    "retried": bool(retried),
                    "step": int(getattr(getattr(agent, "state", None), "n_steps", 0) or 0)})
    except Exception:  # pragma: no cover - telemetry never fails a run
        logger.debug("llm_output_truncated event skipped", exc_info=True)


def build_truncation_note(tokens: Optional[int],
                          reasoning_tokens: Optional[int] = None) -> Any:
    """The control message injected before the single retry — the write note,
    or the reasoning note when the cut was spent thinking."""
    from modules.llm.messages import MessageOrigin, make_control_message
    shown = f"{tokens:,}" if tokens else "the model's maximum"
    if reasoning_bound(tokens, reasoning_tokens):
        text = _REASONING_NOTE.format(tokens=shown, reasoning=f"{reasoning_tokens:,}")
    else:
        text = _NOTE.format(tokens=shown)
    return make_control_message(text, MessageOrigin.INTERVENTION)


async def handle_truncated_output(agent: Any, input_messages: List[Any],
                                  timeout: float, result: Any) -> Any:
    """Return *result*, or a single re-run of the step when the last answer was
    cut at the output budget. Fail-open: any error returns the original result.
    """
    try:
        from modules.llm.output_budget import (last_output_tokens, last_reasoning_tokens,
                                               output_was_truncated)
        if not output_was_truncated(getattr(agent, "llm", None)):
            return result
        tokens = last_output_tokens(getattr(agent, "llm", None))
        reasoning = last_reasoning_tokens(getattr(agent, "llm", None))
    except Exception:  # pragma: no cover - defensive
        return result

    shape = ("reasoning-bound, %s reasoning tokens" % (f"{reasoning:,}" if reasoning is not None else "?")
             if reasoning_bound(tokens, reasoning) else "answer-bound")
    if not truncation_retry_enabled():
        _emit(agent, tokens, retried=False, reasoning_tokens=reasoning)
        logger.warning(
            "LLM output was truncated at %s tokens (finish_reason=length, %s); "
            "LLM_OUTPUT_TRUNCATION_RETRY is off, using the truncated answer",
            tokens, shape)
        return result

    _emit(agent, tokens, retried=True, reasoning_tokens=reasoning)
    logger.warning("LLM output truncated at %s tokens (%s) — retrying the step ONCE "
                   "with the cut named", tokens, shape)
    try:
        import asyncio
        retry_messages = list(input_messages) + [build_truncation_note(tokens, reasoning)]
        return await asyncio.wait_for(
            agent._get_next_action_internal(retry_messages), timeout=timeout)
    except Exception as err:
        # ONE retry, and an honest fall-back to what we already have — a second
        # attempt would double the very cost this exists to bound.
        logger.warning("truncation retry failed (%s); keeping the first answer", err)
        return result
