"""Ops-session finding, 2026-08-27: OpenRouter ran out of credits, but the
credit-death sentinel never latched. Root cause: `get_next_action`'s OWN
provider-fallback (two `except` blocks below) recovers from a credit-death
error IN-METHOD and returns the fallback's successful result — the original
exception never propagates to `error_recovery.py::_handle_step_error`, which
was, until this fix, the ONLY place that called
`_trip_sentinel_if_credit_death`. Net effect: every subsequent LLM call kept
trying the dead provider first, failing with a real 402, then silently
falling back again — forever, invisibly (billing-failover masked it as a
clean `done`).

Fix: both except blocks in `get_next_action` now call
`self._trip_sentinel_if_credit_death(...)` themselves, before attempting
fallback — mirroring error_recovery.py's own "trip BEFORE any branching"
rule. The method is idempotent/no-op unless the error actually looks like
credit death (see core/credit_sentinel.py), so this is safe to call from
every catch site that might swallow such an error.

A full behavioral test would need to mock `get_next_action`'s entire
call-timeout/vision/action-model preamble; this file instead pins the
structural invariant directly against the source, in the same style as the
neighboring `test_llm_fallback_excludes_failed.py` regression test.
"""
import agents.task.agent.core.llm_runner as m

_SRC = open(m.__file__).read()


def _except_block(marker: str) -> str:
    """The except block's own source, up to (not including) the next
    top-level `except` at the same indent, or EOF."""
    start = _SRC.index(marker)
    rest = _SRC[start + len(marker):]
    next_except = rest.find("\n\t\texcept ")
    return rest if next_except == -1 else rest[:next_except]


def test_provider_error_block_trips_sentinel_before_fallback():
    block = _except_block(
        "except (LLMRateLimitError, LLMAuthenticationError, LLMConnectionError) as llm_error:")
    assert "self._trip_sentinel_if_credit_death(llm_error)" in block
    trip_pos = block.index("self._trip_sentinel_if_credit_death(llm_error)")
    fallback_pos = block.index("self._get_fallback_llm(")
    assert trip_pos < fallback_pos, (
        "the sentinel must be tripped BEFORE the fallback attempt — a "
        "successful fallback returns straight out of this except block and "
        "never reaches a later trip call")


def test_generic_llm_error_block_trips_sentinel_before_fallback():
    block = _except_block("except LLMError as generic_llm_error:")
    assert "self._trip_sentinel_if_credit_death(generic_llm_error)" in block
    trip_pos = block.index("self._trip_sentinel_if_credit_death(generic_llm_error)")
    fallback_pos = block.index("self._get_fallback_llm(")
    assert trip_pos < fallback_pos


def test_trip_sentinel_method_is_the_shared_error_recovery_one():
    """Both call sites must invoke the SAME idempotent method
    `error_recovery.py` already defines — not a new/duplicate implementation."""
    from agents.task.agent.service import Agent
    from agents.task.agent.core.error_recovery import ErrorRecoveryMixin
    assert Agent._trip_sentinel_if_credit_death.__qualname__.startswith(
        "ErrorRecoveryMixin")
