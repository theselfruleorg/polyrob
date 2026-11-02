"""F8 (prod review 2026-10-29): the LLM-compaction cooldown stamp belongs to ONE run.

The stamp is a step number and the step counter restarts every run (every owner
turn). In session 82b74d81 (10-28) a stamp from an earlier turn read "compacted
-7 step(s) ago and it cut 1%" and held the cooldown while the gauge climbed from
85 % to 98 %.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from test_step_impl_characterization import _build_agent  # same dir on sys.path


# --- the step ladder: a cooldown stamp belongs to ONE run ---------------------

def _compaction_agent(usage_pct: float, last_savings=None):
    a = _build_agent(done=False, validate=True)
    mm = a.message_manager
    mm.get_context_usage_percent.return_value = usage_pct
    mm.llm_compact_history = AsyncMock(return_value=True)
    mm.emergency_context_prune = MagicMock()
    mm.last_compaction_savings = MagicMock(return_value=last_savings)
    return a


@pytest.mark.asyncio
async def test_a_stamp_from_an_earlier_run_does_not_hold_the_cooldown():
    a = _compaction_agent(usage_pct=90.0, last_savings=0.01)
    mm = a.message_manager
    a.state.n_steps = 12            # turn 1, step 12: compacts and stamps 12
    await a._prepare_step()
    assert mm.llm_compact_history.await_count == 1
    a.state.n_steps = 5             # turn 2 (n_steps restarted): "compacted -7 steps ago"
    await a._prepare_step()
    assert mm.llm_compact_history.await_count == 2, (
        "a cooldown stamp from the previous turn blocked compaction in this turn")


@pytest.mark.asyncio
async def test_the_first_step_of_a_run_is_never_inside_an_old_cooldown():
    a = _compaction_agent(usage_pct=90.0, last_savings=None)
    mm = a.message_manager
    a.state.n_steps = 1
    await a._prepare_step()         # turn 1, step 1: compacts, stamps 1
    assert mm.llm_compact_history.await_count == 1
    a.state.n_steps = 1             # turn 2, step 1: same number, a new run
    await a._prepare_step()
    assert mm.llm_compact_history.await_count == 2
