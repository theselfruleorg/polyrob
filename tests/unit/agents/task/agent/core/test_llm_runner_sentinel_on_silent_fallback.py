"""A credit-dead provider that recovers through the runner's own fallback never raises to
``_handle_step_error``, so the runner trips the credit sentinel itself, BEFORE the ladder
walk. Otherwise every later call tries the dead provider first, forever, invisibly.

This pins the order against the source, in the same style as the neighboring
``test_llm_fallback_excludes_failed.py``.
"""
import inspect

import agents.task.agent.core.llm_runner as m

_SRC = open(m.__file__).read()


def test_the_failure_policy_trips_the_sentinel_before_the_ladder_walk():
    block = inspect.getsource(m.LLMRunnerMixin._fail_over_or_raise)
    trip_pos = block.index("self._trip_sentinel_if_credit_death(llm_error)")
    fallback_pos = block.index("self._get_fallback_llm(")
    assert trip_pos < fallback_pos, (
        "a successful fallback returns straight out of this method and never reaches "
        "a later trip call")


def test_trip_sentinel_method_is_the_shared_error_recovery_one():
    """Both call sites must invoke the SAME idempotent method
    `error_recovery.py` already defines — not a new/duplicate implementation."""
    from agents.task.agent.service import Agent
    from agents.task.agent.core.error_recovery import ErrorRecoveryMixin
    assert Agent._trip_sentinel_if_credit_death.__qualname__.startswith(
        "ErrorRecoveryMixin")
