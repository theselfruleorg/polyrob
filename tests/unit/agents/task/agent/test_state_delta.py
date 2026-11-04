"""095 W4 — the ONE ``<state-delta>`` channel (``MessageManager.update_state_delta``).

R12: changed state goes at the END of the context, in full, only when it
changed; the cached prefix (system prompt + foundation + earlier history) is
never rewritten. Plus the parts it carries: self-facts after a model change,
the owner's style / owner.md / SELF doc at the step boundary, and the skill
index after load_tool (IN-4: a skill whose every declared tool is absent is not
listed; IN-12: owner-only skills are not listed on a third-party seat).
"""
import logging
import types
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from modules.llm.messages import AIMessage
from tests.support.prefix_cache import serialize_message


def _mm() -> MessageManager:
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(llm=llm, task="Test task", action_descriptions="acts",
                          system_prompt_class=SystemPrompt, max_input_tokens=4000)


def _deltas(mm):
    return [str(m.message.content) for m in mm.history.messages
            if "<state-delta " in str(m.message.content)]


def test_hash_dedupe_and_render_clock_ignored():
    mm = _mm()
    assert mm.update_state_delta({"live_health": "Live health as of 2026-10-30 10:00 UTC:\nok"})
    # Same facts, a later render stamp: nothing new.
    assert not mm.update_state_delta({"live_health": "Live health as of 2026-10-30 10:05 UTC:\nok"})
    assert not mm.update_state_delta({"live_health": None})
    assert len(_deltas(mm)) == 1
    assert mm.update_state_delta({"live_health": "Live health as of 2026-10-30 10:06 UTC:\nDEGRADED"})
    assert len(_deltas(mm)) == 2


def test_only_the_changed_part_is_pushed_in_full():
    mm = _mm()
    mm.update_state_delta({"live_health": "H1", "operations": "O1"})
    mm.update_state_delta({"live_health": "H1", "operations": "O2 full text"})
    last = _deltas(mm)[-1]
    assert "<operations>\nO2 full text\n</operations>" in last
    assert "<live-health>" not in last
    assert last.startswith("<system-note>\n<state-delta as_of=")


def test_end_placement_and_cache_prefix_bytes_unchanged():
    mm = _mm()
    mm._add_message_with_tokens(AIMessage(content="earlier turn"), _internal=True)
    before = [serialize_message(m) for m in mm.get_messages_for_llm(consume_ephemeral=False)]
    system_before = mm._system_message.content
    assert mm.update_state_delta({"operations": "Jobs: none"})
    after = [serialize_message(m) for m in mm.get_messages_for_llm(consume_ephemeral=False)]
    assert mm._system_message.content == system_before
    assert after[:len(before)] == before          # the whole prefix is byte-identical
    assert len(after) == len(before) + 1
    assert "<state-delta " in after[-1]           # the delta is the LAST message
    assert "<state-delta " in str(mm.history.messages[-1].message.content)


def test_per_part_cap_with_a_tail():
    mm = _mm()
    mm.update_state_delta({"owner_rules": "x" * (MessageManager.STATE_DELTA_PART_CAP + 500)})
    body = _deltas(mm)[0]
    assert "[cut — agent_status or the part's own tool has the rest]" in body
    assert body.count("x") <= MessageManager.STATE_DELTA_PART_CAP + 10


def test_pushed_again_when_the_carrier_left_history():
    mm = _mm()
    mm.update_state_delta({"operations": "Jobs: none"})
    mm.history.messages.clear()  # compaction / eviction dropped it
    assert mm.update_state_delta({"operations": "Jobs: none"})


def test_baseline_records_without_pushing():
    mm = _mm()
    assert not mm.update_state_delta({"style_prefs": "Style: none"}, baseline=True)
    assert not mm.update_state_delta({"style_prefs": "Style: none"})
    assert mm.update_state_delta({"style_prefs": "Style: verbosity terse"})


def test_empty_part_scrubs_every_earlier_copy():
    mm = _mm()
    mm.update_state_delta({"operations": "SECRET_OPS", "live_health": "H"})
    mm.update_state_delta({"operations": ""})
    joined = "\n".join(_deltas(mm))
    assert "SECRET_OPS" not in joined and "<live-health>\nH\n</live-health>" in joined


# --- the parts ------------------------------------------------------------------

def test_model_change_pushes_self_facts():
    from agents.task.agent.core.model_swap import ModelSwapMixin
    mm = _mm()
    agent = types.SimpleNamespace(message_manager=mm, model_name="m-blind",
                                  llm_provider="openrouter", use_vision=False,
                                  session_id="s", use_native_tools=True,
                                  logger=logging.getLogger("t"))
    ModelSwapMixin._refresh_after_model_change(agent, "fallback", "m-vision", reason="429")
    body = _deltas(mm)[-1]
    assert '<self-facts>\nModel now: "m-blind" via provider "openrouter"' in body
    assert 'changed from "m-vision"' in body and "not the owner" in body
    assert "you cannot see images" in body


def test_style_write_applies_from_the_next_step(monkeypatch, tmp_path):
    import core.runtime_paths as rp
    from agents.task.agent.core import live_health, state_delta
    from core.prefs import write_preference
    from core.prefs_hooks import notify
    monkeypatch.setattr(rp, "prefs_home_dir", lambda: tmp_path)
    monkeypatch.setattr(live_health, "_operations_injected", lambda agent: True)
    mm = _mm()
    agent = types.SimpleNamespace(message_manager=mm, user_id="u1",
                                  orchestrator=types.SimpleNamespace(user_id="u1"))
    state_delta.refresh_self_context(agent)            # baseline: the foundation has it
    assert _deltas(mm) == []
    state_delta.refresh_self_context(agent)            # nothing changed
    assert _deltas(mm) == []
    ok, err = write_preference(tmp_path, "u1", "style.verbosity", "terse")
    assert ok, err
    notify("prefs")
    state_delta.refresh_self_context(agent)
    out = _deltas(mm)
    assert len(out) == 1 and "verbosity terse" in out[0] and "<style-prefs>" in out[0]
    assert "<owner-rules>" not in out[0]               # unchanged parts stay out


def test_style_part_withheld_on_a_third_party_seat(monkeypatch, tmp_path):
    import core.runtime_paths as rp
    from agents.task.agent.core import live_health, state_delta
    monkeypatch.setattr(rp, "prefs_home_dir", lambda: tmp_path)
    monkeypatch.setattr(live_health, "_operations_injected", lambda agent: False)
    mm = _mm()
    agent = types.SimpleNamespace(message_manager=mm, user_id="u1",
                                  orchestrator=types.SimpleNamespace(user_id="u1"))
    state_delta.refresh_self_context(agent)
    assert _deltas(mm) == []


def test_load_tool_pushes_the_skill_index_change(monkeypatch):
    from agents.task.agent import skill_manager as smod
    from agents.task.agent.core import state_delta
    listed = {"ids": ["a"]}

    class _SM:
        def declared_tool_ids(self, user_id):
            return set()

        def get_catalog_skills(self, **kw):
            return [smod.MatchedSkill(skill_id=i, priority=5, match_reasons=[], content="",
                                      when=f"when {i}") for i in listed["ids"]]

    monkeypatch.setattr(smod, "get_skill_manager", lambda: _SM())
    controller = types.SimpleNamespace(
        _skill_index_stale=False, _session_skills={"a": object()}, _auto_skills=set(),
        list_tools=lambda: ["defi_data"], absent_tool_ids=lambda ids: set())
    agent = types.SimpleNamespace(controller=controller,
                                  orchestrator=types.SimpleNamespace(user_id="u"))
    assert state_delta.skills_delta_part(agent) is None    # no load_tool since
    controller._skill_index_stale = True
    listed["ids"] = ["a", "trading-x"]
    text = state_delta.skills_delta_part(agent)
    assert "trading-x" in text and "load_skill" in text
    assert "trading-x" in controller._session_skills       # load_skill serves it
    assert controller._skill_index_stale is False
    # An id that leaves the index leaves the session set (load_skill re-gates it).
    controller._skill_index_stale = True
    listed["ids"] = ["trading-x"]
    text = state_delta.skills_delta_part(agent)
    assert "No longer in your skill index: a" in text
    assert "a" not in controller._session_skills


def test_gate_withholds_a_skill_whose_every_declared_tool_is_absent():
    from core.skills.catalog import gate_allows
    rule = {"triggers": {"tool_ids": ["twitter", "x_browser"]}}
    assert gate_allows(rule, ["filesystem"], listing=True)
    assert not gate_allows(rule, ["filesystem"], listing=True,
                           absent={"twitter", "x_browser"})
    assert gate_allows(rule, ["filesystem"], listing=True, absent={"twitter"})
    # A listing view (no session) is not filtered.
    assert gate_allows(rule, None, listing=True)


def test_owner_only_skills_are_not_listed_on_a_third_party_seat():
    """The frontmatter key ``polyrob-owner-only`` is the ONE source (M6)."""
    from core.skills.catalog import gate_allows, rule_from_frontmatter
    rule = rule_from_frontmatter({"description": "d",
                                  "metadata": {"polyrob-owner-only": "true"}})
    assert rule["owner_only"] is True
    assert not gate_allows(rule, [], listing=True, third_party=True)
    assert gate_allows(rule, [], listing=True)
    plain = rule_from_frontmatter({"description": "d", "metadata": {"polyrob-priority": "3"}})
    assert "owner_only" not in plain and gate_allows(plain, [], third_party=True)
