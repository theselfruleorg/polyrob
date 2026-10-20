"""B1 recall quality (prod 72h, 563 recalls).

1. Cron recall was blind: the owner-rules overlay leads the cron task, so 85% of
   cron recalls searched the overlay's words. The query now comes from the job's
   own text (``recall_seed``), never from the overlay.
2. Chat recall was stale: a long-lived chat session kept ``self.task`` = its
   FIRST message, so every later turn recalled for that first message. The query
   now comes from the latest genuine owner message of the turn.
3. Recall cost: ~25,000 chars per injection. The injection is now bounded and a
   later cadence step never re-injects a line this run already saw.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from agents.task.agent.core.memory_prefetch import (
    MemoryPrefetchMixin,
    RECALL_KB_MAX_CHARS,
    RECALL_MEMORY_MAX_CHARS,
    build_prefetch_message,
)
from agents.task.agent.core.user_ingress import latest_turn_text
from modules.memory.provider import NullMemoryProvider
from modules.memory.registry import reset_memory_registry, set_external_memory_provider

OVERLAY = ("OWNER RULES NEWER THAN THIS JOB'S TEXT (the job was written 2026-09-18). "
           "They OVERRIDE the job text below wherever they conflict — follow the owner "
           "rule, not the job:\n- Approve posts with me")


class _Stub(MemoryPrefetchMixin):
    def __init__(self, task, *, seed=None, turn=None, brain=None, n_steps=1):
        self.task = task
        self._last_brain_state = brain
        self.state = SimpleNamespace(n_steps=n_steps)
        self.orchestrator = SimpleNamespace(_recall_seed=seed)
        if turn is not None:
            self._recall_turn_text = turn


# ---- 1. cron: the query is the job's own text -------------------------------

def test_cron_query_uses_the_seed_not_the_overlay():
    job = "PNL BUYBACK every 4 hours, fixed size 0.05 ETH"
    q = _Stub(f"{OVERLAY}\n\n{job}", seed=job)._build_recall_query()
    assert "OWNER RULES" not in q and "OVERRIDE" not in q
    assert "PNL BUYBACK" in q


def test_seed_still_carries_the_brain_next_goal():
    q = _Stub("x", seed="buyback job", n_steps=3,
              brain=SimpleNamespace(next_goal="quote the swap"))._build_recall_query()
    assert "buyback job" in q and "quote the swap" in q


def test_d7_stale_brain_from_the_previous_run_is_ignored_at_step_one():
    """D7 / A3: at step 1 the brain is the PREVIOUS run's ("End the turn - the
    answer is delivered …") and polluted the query."""
    q = _Stub("bridge status on base", n_steps=1,
              brain=SimpleNamespace(next_goal="End the turn - the answer is delivered")
              )._build_recall_query()
    assert "End the turn" not in q and "bridge status" in q


def test_d7_short_turn_falls_back_to_the_previous_subject():
    s = _Stub("task text", turn="did the bridge to base arrive?")
    assert "bridge" in s._build_recall_query()
    s._recall_turn_text = "yes, do it"
    q = s._build_recall_query()
    assert "yes, do it" in q and "bridge" in q


def test_d7_short_turn_without_history_falls_back_to_the_task():
    q = _Stub("PNL buyback tranche plan", turn="ok 👍")._build_recall_query()
    assert "PNL buyback tranche plan" in q


def test_d7_non_latin_turn_yields_terms():
    from modules.memory.fts_terms import informative_terms
    assert informative_terms("Сколько стоит мост на Base?") == ["Сколько", "стоит", "мост", "Base"]
    assert "café" in informative_terms("le café décaféiné")


def test_no_seed_falls_back_to_task():
    assert _Stub("do the task")._build_recall_query() == "do the task"


@pytest.mark.asyncio
async def test_cron_request_carries_the_job_text_as_recall_seed(tmp_path):
    from cron.jobs import CronJob
    from cron.runner import make_agent_runner
    from core.instance import resolve_instance_id, self_tier_root
    root = self_tier_root(tmp_path, "u1", resolve_instance_id())
    root.mkdir(parents=True)
    (root / "owner.md").write_text(
        'APPROVE POSTS (2026-10-05 17:56): owner said "Approve posts with me." '
        "[from: owner said 2026-10-05]\n", encoding="utf-8")
    captured = {}

    class _TaskAgent:
        async def create_session(self, user_id, request):
            captured["request"] = request
            return {"id": "s1"}

        async def run_session(self, user_id, session_id):
            return "ok"

    runner = make_agent_runner(_TaskAgent(), data_dir=str(tmp_path))
    job = CronJob(id="j", task="PROMO: post one post.", schedule_spec="1h", user_id="u1",
                  next_run_at=None, payload={"provider": "anthropic"},
                  created_at=datetime(2026, 10, 4, 7, 38))
    assert await runner(job) is True
    req = captured["request"]
    assert req["task"].startswith("OWNER RULES")
    assert req["recall_seed"] == "PROMO: post one post."


def test_session_request_persists_the_seed():
    from agents.task.task_agent_support import SessionRequest
    req = SessionRequest(task="t", provider="p", model="m", recall_seed="job text")
    assert req.__dict__["recall_seed"] == "job text"


def test_goal_request_carries_a_recall_seed():
    import inspect
    from agents.task.goals import goal_run
    assert '"recall_seed"' in inspect.getsource(goal_run)


# ---- 2. chat: the query is the latest owner message of the turn --------------

def test_chat_query_uses_the_latest_turn_text_not_the_first_message():
    q = _Stub("why that was rejected? explain",
              turn="what is the buyback status now")._build_recall_query()
    assert "buyback status" in q
    assert "rejected" not in q


def test_latest_turn_text_takes_the_last_genuine_message():
    msgs = [{"kind": "continuation", "text": "first"},
            {"kind": "comment", "text": "second, newest"}]
    assert latest_turn_text(msgs) == "second, newest"


def test_latest_turn_text_ignores_a_forged_batch():
    assert latest_turn_text([{"kind": "self_wake", "text": "wake up"}]) is None
    assert latest_turn_text([]) is None


# ---- 3. cost: bounded injection, no repeat inside a run ----------------------

class _Provider(NullMemoryProvider):
    def __init__(self, recalled, kb=""):
        self._recalled, self._kb = recalled, kb

    @property
    def is_external(self) -> bool:
        return True

    async def prefetch(self, query, *, session_id, user_id=None):
        return self._recalled

    async def kb_search(self, query, *, user_id=None, collection="default", limit=8):
        self.kb_limit = limit
        return self._kb


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_memory_registry()
    yield
    reset_memory_registry()


def _rows(n, size, tag="m"):
    return "\n".join(f"- [{tag}{i}] " + ("x" * size) for i in range(n))


@pytest.mark.asyncio
async def test_injection_is_bounded(monkeypatch):
    from core.config_policy import AutonomyConfig
    monkeypatch.setattr(AutonomyConfig, "kb_auto_prefetch", staticmethod(lambda: True))
    kb = "\n".join(f"[doc.md #{i}] " + ("k" * 2000) for i in range(8))
    prov = _Provider(_rows(5, 4000), kb)
    set_external_memory_provider(prov)
    msg = await build_prefetch_message("buyback", session_id="s1")
    assert msg is not None
    budget = RECALL_MEMORY_MAX_CHARS + RECALL_KB_MAX_CHARS
    assert budget <= 10_000
    # the envelope adds a little; the payload stays inside the budget
    assert len(msg.content) < budget + 1500
    assert "[m0]" in msg.content and "[doc.md #0]" in msg.content
    assert prov.kb_limit <= 4


@pytest.mark.asyncio
async def test_a_later_step_never_reinjects_a_seen_line():
    set_external_memory_provider(_Provider(_rows(3, 50)))
    seen = set()
    first = await build_prefetch_message("buyback", session_id="s1", seen=seen)
    assert first is not None and "[m2]" in first.content
    assert await build_prefetch_message("buyback", session_id="s1", seen=seen) is None


@pytest.mark.asyncio
async def test_only_new_lines_ride_a_later_step():
    prov = _Provider(_rows(2, 50))
    set_external_memory_provider(prov)
    seen = set()
    await build_prefetch_message("buyback", session_id="s1", seen=seen)
    prov._recalled = _rows(3, 50)
    msg = await build_prefetch_message("buyback", session_id="s1", seen=seen)
    assert msg is not None
    assert "[m2]" in msg.content and "[m0]" not in msg.content
