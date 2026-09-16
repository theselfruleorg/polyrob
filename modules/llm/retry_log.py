"""Shared per-attempt / final-exhaustion log rendering for the LLM
executor's retry/fallback cascade (A12 fix round 5, 2026-09-14).

Round 2 (043 A12, 2026-09-14) downgraded a cluster of per-attempt failure
logs from ERROR to WARNING across three files -- ``modules/llm/adapters.py``,
``modules/llm/openai_client.py``, and
``agents/task/agent/core/next_action_internal.py`` -- so the REPL's
ERROR-level console handler only ever shows the retry loop's own single
final-exhaustion line, never an intermediate attempt. Every per-attempt
failure in this cascade (a native-tools attempt, a structured-output
attempt, a plain-fallback attempt, the adapter's own error classification)
is PROVISIONAL: the executor's outer retry loop
(``next_action_internal.py``) is the only layer that knows whether this was
the LAST attempt, so it alone owns the turn's single final ERROR line.
Nothing upstream of it should ever be console ERROR.

Each round-2 call site carried its own multi-line "why WARNING not ERROR"
comment repeating this reasoning, which pushed ``adapters.py`` and
``openai_client.py`` over their file-size ratchet ceilings
(``tests/test_file_size_ratchet.py``) -- this module is the ONE place that
reasoning lives now; call sites carry a one-line pointer comment instead.
Behaviour is unchanged from round 2: same messages, same levels, same call
order -- this is a pure code-motion extraction.
"""

from __future__ import annotations

import logging


def log_attempt(logger: logging.Logger, message: str) -> None:
    """Log one leg of the retry/fallback cascade failing. Never the turn's
    last word — the caller already re-raises or falls through to the next
    leg. See this module's docstring for why this is WARNING, not ERROR.
    """
    logger.warning(message)


def log_attempt_classified(logger: logging.Logger, exc: Exception, translated: Exception) -> None:
    """Classify a per-attempt LLM-call failure (the ``translated`` form
    ``modules.llm.error_translation.translate_llm_error`` produces) and log
    ONE line at WARNING describing which category it fell into. Moved
    verbatim from ``LLMClientAdapter._agenerate``'s except block — same five
    branches, same messages, same order, same WARNING level.
    """
    from core.exceptions import (
        LLMPermanentError, LLMRateLimitError, LLMAuthenticationError,
        LLMContextLengthError, LLMConnectionError,
    )
    if isinstance(translated, LLMPermanentError):
        logger.warning(f"Detected PERMANENT error (no fallback): {str(exc)[:200]}")
    elif isinstance(translated, LLMRateLimitError):
        logger.warning(f"Detected rate limit error: {str(exc)[:200]}")
    elif isinstance(translated, LLMAuthenticationError):
        logger.warning(f"Detected authentication error: {str(exc)[:200]}")
    elif isinstance(translated, (LLMContextLengthError, LLMConnectionError)):
        logger.warning(f"Detected {type(translated).__name__}: {str(exc)[:200]}")
    else:
        logger.warning(f"Unexpected LLM error (propagating): {type(exc).__name__}: {str(exc)}")


def log_exhausted(logger: logging.Logger, *, retry_count: int, provider: str, exc: Exception) -> None:
    """The retry loop has genuinely given up — the ONE line a console at
    ERROR level shows for this failure. Names the provider and the error
    class (043 §4.6's requirement), not just a bare attempt count. Moved
    verbatim from ``next_action_internal.py``'s outer retry loop.
    """
    logger.error(
        f"LLM call failed after {retry_count} attempts for provider={provider}: "
        f"{type(exc).__name__}: {str(exc)[:200]}",
        exc_info=True,
    )
