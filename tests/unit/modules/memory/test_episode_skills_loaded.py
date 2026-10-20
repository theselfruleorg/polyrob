"""080 WS-5: the skills a run had active go on the episode meta."""
from types import SimpleNamespace

import pytest

from modules.memory import episodic


def test_skills_loaded_reads_the_activated_set_without_ack_markers():
    ctl = SimpleNamespace(_activated_skills={"b-skill", "a-skill", "a-skill#ack"})
    sub = SimpleNamespace(_is_sub_agent=True,
                          controller=SimpleNamespace(_activated_skills={"leaf-only"}))
    main = SimpleNamespace(controller=SimpleNamespace(_activated_skills={"c-skill"}))
    orch = SimpleNamespace(controller=ctl, agents={"m": main, "s": sub})
    assert episodic.skills_loaded(orch) == ["a-skill", "b-skill", "c-skill"]
    assert episodic.skills_loaded(None) == []
    assert episodic.skills_loaded(object()) == []


@pytest.mark.asyncio
async def test_collect_provenance_carries_skills_loaded():
    orch = SimpleNamespace(controller=SimpleNamespace(_activated_skills={"x"}), agents={})
    out = await episodic.collect_provenance(orch)
    assert out["skills_loaded"] == ["x"]


@pytest.mark.asyncio
async def test_finalize_stamps_skills_loaded_on_meta(monkeypatch):
    from core.config_policy import AutonomyConfig
    monkeypatch.setattr(AutonomyConfig, "episodic_memory_enabled", staticmethod(lambda: True))
    seen = {}

    async def _record(rec, *, session_id, user_id):
        seen["meta"] = rec.meta

    import modules.memory.registry as reg
    monkeypatch.setattr(reg, "memory_record_episode", _record)
    await episodic.finalize_episode(session_id="s", user_id="u", kind="cron",
                                    outcome="done", meta={"source": "cron", "job_id": "j"},
                                    skills_loaded=["b", "a", "a"])
    assert seen["meta"] == {"source": "cron", "job_id": "j", "skills_loaded": ["a", "b"]}
    await episodic.finalize_episode(session_id="s", user_id="u", kind="cron",
                                    outcome="done", meta={"source": "cron"})
    assert "skills_loaded" not in seen["meta"]


# ---- 081 WS-4.6: auto (pinned at start) vs explicit (load_skill), per regime ----

def test_skills_activation_reads_the_pins_and_the_regime():
    main = SimpleNamespace(controller=SimpleNamespace(
        _activated_skills={"dca", "web-research"}, _auto_skills={"dca", "token-identity"},
        _skill_regime="cron"))
    sub = SimpleNamespace(_is_sub_agent=True, controller=SimpleNamespace(
        _auto_skills={"leaf-only"}, _skill_regime="child"))
    orch = SimpleNamespace(controller=None, agents={"m": main, "s": sub})
    assert episodic.skills_activation(orch) == {"auto": ["dca", "token-identity"],
                                                "regime": "cron"}
    assert episodic.skills_activation(None) == {}
    assert episodic.skills_activation(SimpleNamespace(controller=None, agents={})) == {}


@pytest.mark.asyncio
async def test_finalize_stamps_auto_and_regime(monkeypatch):
    from core.config_policy import AutonomyConfig
    monkeypatch.setattr(AutonomyConfig, "episodic_memory_enabled", staticmethod(lambda: True))
    seen = {}

    async def _record(rec, *, session_id, user_id):
        seen["meta"] = rec.meta

    import modules.memory.registry as reg
    monkeypatch.setattr(reg, "memory_record_episode", _record)
    await episodic.finalize_episode(
        session_id="s", user_id="u", kind="chat", outcome="done", meta={"source": "session_end"},
        skills_loaded=["web-research"], skills_activation={"regime": "owner_chat"})
    assert seen["meta"] == {"source": "session_end", "skills_loaded": ["web-research"],
                            "skills_regime": "owner_chat"}
    await episodic.finalize_episode(
        session_id="s", user_id="u", kind="cron", outcome="done", meta={},
        skills_loaded=["dca", "web-research"],
        skills_activation={"auto": ["dca"], "regime": "cron"})
    assert seen["meta"]["skills_auto"] == ["dca"] and seen["meta"]["skills_regime"] == "cron"


def test_construction_stamps_the_pins_and_the_regime():
    import inspect
    from agents.task.agent.core import construction
    src = inspect.getsource(construction)
    assert "self.controller._auto_skills = {s.skill_id for s in _session_matched}" in src
    assert "self.controller._skill_regime = session_regime(self)" in src
