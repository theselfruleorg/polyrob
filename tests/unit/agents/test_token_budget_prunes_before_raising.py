"""The pre-LLM token check prunes once before it kills the run.

Prod evidence (2026-09-18 14:34Z, cron X TARGET COLLECTION `966d3539`): the
step-boundary compaction gate read 74% (pre-recalibration estimates for five
~50k-char anysite results), the recalibrated pre-LLM check then counted
198,365 > 190,000 and raised `LLMResponseError: Token overflow` — no fallback
provider, run failed at step 5 with the money-adjacent outreach work undone.
The emergency prune (a non-LLM net that keeps the last complete tool-call
pairs) would have brought it under the line; it just never got a turn. So the
pre-LLM check now prunes ONCE on overflow and re-checks, and only a history
that is still over the line raises.
"""
import logging

import pytest

from agents.task.agent.core.token_budget import ensure_token_budget


class _MM:
    def __init__(self, safe_sequence):
        self._seq = list(safe_sequence)
        self.pruned = 0

    def check_token_safety(self, raise_on_overflow=False):
        safe = self._seq.pop(0)
        if raise_on_overflow and not safe:
            from core.exceptions import LLMResponseError
            raise LLMResponseError("Token overflow: 198365 tokens exceeds safe limit 190000")
        return {"safe": safe, "usage_percent": 99.2 if not safe else 60.0,
                "current_tokens": 198365 if not safe else 120000, "max_limit": 200000}

    def emergency_context_prune(self):
        self.pruned += 1


def test_safe_history_is_untouched():
    mm = _MM([True])
    out = ensure_token_budget(mm, logging.getLogger("t"))
    assert out["safe"] and mm.pruned == 0


def test_overflow_prunes_once_then_proceeds():
    mm = _MM([False, True])
    out = ensure_token_budget(mm, logging.getLogger("t"))
    assert out["safe"] and mm.pruned == 1


def test_still_over_after_prune_raises():
    from core.exceptions import LLMResponseError
    mm = _MM([False, False])
    with pytest.raises(LLMResponseError, match="Token overflow"):
        ensure_token_budget(mm, logging.getLogger("t"))
    assert mm.pruned == 1


def test_prune_failure_does_not_mask_the_overflow():
    from core.exceptions import LLMResponseError

    class Broken(_MM):
        def emergency_context_prune(self):
            raise RuntimeError("boom")

    mm = Broken([False, False])
    with pytest.raises(LLMResponseError):
        ensure_token_budget(mm, logging.getLogger("t"))
