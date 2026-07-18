"""Regression (P1 finalization): two provider-fallback branches in llm_runner drifted
— the provider-error branch excluded only the current provider, while the generic-
LLMError branch excluded current + all already-failed providers. The narrower one could
fall back straight to a provider already known dead this run. Both must exclude
self.state.llm_providers_failed.
"""
import re

import agents.task.agent.core.llm_runner as m


def test_all_fallback_calls_exclude_previously_failed_providers():
    src = m.__loader__.get_source(m.__name__) if hasattr(m, "__loader__") else open(m.__file__).read()
    # Every _get_fallback_llm(...) exclude_providers must include llm_providers_failed.
    # (match to end of line — the failed list is appended after the [current] bracket)
    for call in re.findall(r"exclude_providers=.*", src):
        assert "llm_providers_failed" in call, (
            f"fallback exclude list must include already-failed providers: {call}"
        )
    # And there must be at least the two known call sites.
    assert src.count("exclude_providers=") >= 2
