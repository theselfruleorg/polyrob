"""116 W9.3: a helper's usage is summed in memory and never written a second time.

The helper's own calls are rows in ``usage_records`` under its virtual session; a
``sub_agent_aggregate`` row in the parent session made every window total count the
helper twice (written only for a credit-billed tenant)."""
from types import SimpleNamespace

import pytest

from agents.task.agent.sub_agent_manager import SubAgentManager


class _DB:
    def __init__(self):
        self.sql = []

    async def execute(self, sql, params=()):
        self.sql.append(sql)


class _Tracker:
    def __init__(self):
        self.db = _DB()

    async def get_session_breakdown(self, session_id):
        return {"total_credits_charged": 5, "total_api_cost_usd": 0.25,
                "by_type": [{"tokens": {"input": 100, "output": 20, "cached": 3,
                                        "cache_write": 1}, "calls": 2}]}


@pytest.mark.asyncio
async def test_the_helper_usage_is_summed_and_not_written_again():
    tracker = _Tracker()
    mgr = SubAgentManager.__new__(SubAgentManager)
    mgr.orchestrator = SimpleNamespace(usage_tracker=tracker, user_id="u1")
    mgr.session_id = "s1"
    mgr.logger = SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None,
                                 warning=lambda *a, **k: None)
    from agents.task.agent.sub_agent_manager import SubAgentUsage
    mgr._total_usage = SubAgentUsage()
    usage = await mgr._aggregate_usage_to_parent("s1__sub_1", "sub_1", "a task")
    assert tracker.db.sql == []
    assert (usage.credits_charged, usage.total_tokens, usage.llm_calls) == (5, 120, 2)
    assert mgr._total_usage.api_cost_usd == pytest.approx(0.25)
    assert mgr._total_usage.total_tokens == 120
