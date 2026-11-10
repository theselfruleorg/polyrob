"""The runner's ladder walk excludes the current provider and every provider that already
failed this run, so it cannot fall back straight to a provider known dead.
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
    assert src.count("exclude_providers=") == 1, "the runner has one ladder walk"
