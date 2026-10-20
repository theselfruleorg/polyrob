"""2026-10-13 coherency review: one regime resolver + one per-regime policy table."""
from types import SimpleNamespace

from core.autonomy_marker import mark_autonomous
from agents.task import session_class as sc


def _agent(sid, **kw):
    return SimpleNamespace(session_id=sid, orchestrator=SimpleNamespace(**kw.pop("orch", {})), **kw)


def test_every_regime_has_a_policy_row():
    assert set(sc.REGIME_POLICY) == set(sc.REGIMES)


def test_unmarked_session_is_owner_chat():
    assert sc.session_regime(_agent("rg-chat")) == sc.OWNER_CHAT


def test_cron_goal_and_bare_autonomous():
    mark_autonomous("rg-cron", None, cron_job_id="job1")
    mark_autonomous("rg-goal", "g1")
    mark_autonomous("rg-wake", None)
    assert sc.session_regime(_agent("rg-cron")) == sc.CRON
    assert sc.session_regime(_agent("rg-goal")) == sc.GOAL
    assert sc.session_regime(_agent("rg-wake")) == sc.AUTONOMOUS


def test_third_party_and_child_win_over_autonomy():
    mark_autonomous("rg-room", None, cron_job_id="job2")
    assert sc.session_regime(_agent("rg-room", orch={"_public_session": True})) == sc.ROOM
    assert sc.session_regime(_agent("rg-cor", orch={"_correspondent_session": True})) == sc.CORRESPONDENT
    assert sc.session_regime(_agent("rg-room", _is_sub_agent=True)) == sc.CHILD


def test_policy_shapes_that_the_fixes_rely_on():
    chat, cron = sc.REGIME_POLICY[sc.OWNER_CHAT], sc.REGIME_POLICY[sc.CRON]
    assert not chat.pin_initial_task and chat.latest_owner_turn_wins
    assert cron.pin_initial_task and cron.recall_own_runs_first and cron.run_record
    assert chat.demote_automation_recall and not cron.demote_automation_recall
    for r in (sc.ROOM, sc.CORRESPONDENT, sc.CHILD):
        assert not sc.REGIME_POLICY[r].owner_memory


def test_a_room_probe_error_falls_to_room_and_says_so(monkeypatch, caplog):
    import logging
    from types import SimpleNamespace
    import core.surfaces.room_policy as rp
    from agents.task import session_class as sc

    def _boom(_o):
        raise RuntimeError("probe down")

    monkeypatch.setattr(rp, "is_public_session", _boom)
    with caplog.at_level(logging.WARNING, logger=sc.__name__):
        assert sc.session_regime(orchestrator=SimpleNamespace(), session_id="s-x") == sc.ROOM
    assert any("room probe failed" in r.getMessage() for r in caplog.records)


# ── 2026-10-15 review rec. 4: REGIME_POLICY.owner_memory is the ONE source ──

def test_may_write_owner_memory_delegates_to_the_regime_column():
    assert sc.may_write_owner_memory(SimpleNamespace(session_id="rg-mw-chat")) is True
    assert sc.may_write_owner_memory(SimpleNamespace(session_id="rg-mw-room",
                                                     _public_session=True)) is False
    assert sc.may_write_owner_memory(SimpleNamespace(session_id="rg-mw-cor",
                                                     _correspondent_session=True)) is False


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_child_leaves_the_shared_findings_queue_to_its_parent(monkeypatch):
    """A CHILD shares its parent's session queue. It must not drain it (a drained
    finding it may not write would be lost); the parent's drain writes it."""
    import logging
    from agents.task.agent.core.memory_writer import MemoryWriterMixin

    synced, drained = [], []

    async def fake_sync(task, content, **kw):
        synced.append(content)
        return "inserted"

    monkeypatch.setattr("modules.memory.registry.memory_sync_turn", fake_sync)
    queue = ["a finding"]

    def drain(sid):
        out, queue[:] = list(queue), []
        drained.append(out)
        return out

    def host(is_child):
        h = MemoryWriterMixin()
        h.orchestrator = None
        h._is_sub_agent = is_child
        h.task_context_manager = SimpleNamespace(
            add_step_memory=lambda **kw: True, get_session=lambda sid: None,
            drain_promoted_findings=drain,
            release_unsent_findings=lambda *a, **k: True)
        h.state = SimpleNamespace(track_finding=lambda: None)
        h.session_id, h.user_id, h.task = "rg-shared", "owner", "task"
        h.logger = logging.getLogger("rg-child")
        return h

    await host(True)._save_step_to_memory(step_number=1, brain_state={"memory": "x"},
                                          actions=[], results=[])
    assert drained == [] and synced == [] and queue == ["a finding"]
    await host(False)._save_step_to_memory(step_number=1, brain_state={"memory": "x"},
                                           actions=[], results=[])
    assert synced == ["a finding"]
