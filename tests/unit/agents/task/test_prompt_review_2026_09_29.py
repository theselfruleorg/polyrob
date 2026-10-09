"""Prompt fixes from the 2026-09-29 prompt/skill review (B1, C2, F1, F6, F7, F11)."""
from types import SimpleNamespace

import pytest

from agents.task.agent.prompts import SystemPrompt


def _dm(**extra):
    return {"surface_id": "telegram", "max_message_bytes": 4096, "media_out": True,
            "supports_interactive_ask": True, "chat_type": "dm", **extra}


def _text(**kw):
    kw.setdefault("tool_ids", [])
    return SystemPrompt("actions", use_native_tools=True, **kw).get_system_message().content


# --- B1 -----------------------------------------------------------------------

def test_owner_session_is_taught_propose_action_and_present_choice():
    text = _text(surface=_dm())
    assert "propose_action(command=...)" in text
    assert "Never write the command for them to type" in text
    assert "present_choice(question, options)" in text
    # the one-token rule is for approvals only
    assert "An approval you ask them for must be ONE tappable token" in text
    assert "never assemble it yourself" not in text


def test_autonomous_session_gets_no_present_choice():
    text = _text(autonomous=True)
    assert "propose_action(command=...)" in text
    assert "present_choice" not in text


def test_room_gets_no_owner_moves():
    text = _text(surface=_dm(chat_type="group", chat_name="den"))
    assert "propose_action" not in text


def test_communication_does_not_claim_send_message_is_the_only_verb():
    text = _text()
    assert "only verb" not in text
    assert "done(text) never reaches them" in text


# --- C2 -----------------------------------------------------------------------

def test_correspondent_dm_is_not_framed_as_the_owner_chat():
    text = _text(surface=_dm(correspondent=True))
    assert "private chat with your owner" not in text
    assert "with a correspondent, NOT your owner" in text
    assert "The verbs that exist on this surface" not in text
    assert "propose_action" not in text


def test_surface_profile_correspondent_stamp_matches_correspondent_facing():
    """binding.py cannot import agents (layering); pin it to the SSOT predicate."""
    from agents.task.session_class import correspondent_facing
    import core.surfaces.binding as binding

    class _Registry:
        def resolve(self, key):
            return {"surface_id": "telegram", "chat_id": "1"}

        def get(self, sid):
            return SimpleNamespace(capabilities=SimpleNamespace(max_message_bytes=10))

    class _Container:
        def get_service(self, name):
            return _Registry()

    for stamps in ({}, {"_correspondent_session": True}, {"_correspondent_tainted": True}):
        orch = SimpleNamespace(_chat_session_key="telegram:u:1:dm",
                               container=_Container(), **stamps)
        prof = binding.surface_profile(orch)
        assert prof["correspondent"] == correspondent_facing(orch), stamps


# --- F1 -----------------------------------------------------------------------

def test_browser_section_names_real_actions():
    text = _text(tool_ids=["browser"])
    assert "browser_click_element(index)" in text
    assert "browser_input_text(index, text)" in text
    assert "browser_click(" not in text and "browser_type(" not in text
    assert "browser_screenshot" not in text
    # F11: no route to a tool the session lacks
    import re
    section = re.search(r"<browser-tools>.*?</browser-tools>", text, re.S).group(0)
    assert "fetch_url" not in section and "perplexity" not in section


# --- F6 -----------------------------------------------------------------------

def test_one_file_delivery_rule(monkeypatch):
    import core.config_policy as cp
    monkeypatch.setattr(cp, "message_tool_enabled", lambda: True)
    text = _text(surface=_dm())
    assert "ABSOLUTE workspace path in your" in text
    assert "message(media_paths=[...])" in text
    assert "Writing a file path into your reply is NOT a delivery" not in text


# --- F7 -----------------------------------------------------------------------

def test_owner_instructions_name_only_enabled_lanes(monkeypatch):
    import core.config_policy as cp
    monkeypatch.setattr(cp, "prefs_tool_enabled", lambda: True)
    monkeypatch.setattr(cp.AutonomyConfig, "owner_doc_writable", staticmethod(lambda: False))
    monkeypatch.setattr(cp.AutonomyConfig, "self_context_writable", staticmethod(lambda: False))
    text = _text()
    assert "<owner-instructions>" in text
    assert "`preferences`" in text
    assert "owner_doc_manage" not in text
    assert "self_context_manage" not in text


# --- F11 ----------------------------------------------------------------------

def test_todo_complete_uses_pattern_and_is_gated():
    assert "task_todo_complete(pattern=" in _text(tool_ids=["task"])
    assert "task_todo_complete(id=" not in _text(tool_ids=["task"])
    assert "task_todo_add" not in _text(tool_ids=[])


def test_large_content_matches_the_offload_path():
    from agents.task.robust_parse_config import RobustParseConfig as R
    text = _text()
    assert f"{R.MAX_EXTRACTED_CONTENT_SIZE:,} chars" in text
    assert "[LARGE CONTENT STORED]" in text
    assert "filesystem_read_file(file_path=" in text
    assert ">2M" not in text


def test_status_examples_follow_the_loaded_tools():
    assert "e.g. goal_list" in _text(tool_ids=["goal"])
    assert "goal_list" not in _text(tool_ids=["filesystem"])


def test_tool_catalog_named_only_when_pinned(monkeypatch):
    import core.config_policy as cp
    import core.config_policy.capability_toggles as ct
    monkeypatch.setattr(cp, "tool_progressive_disclosure", lambda: False)
    monkeypatch.setattr(ct, "autonomous_tool_disclosure", lambda: False)
    assert "<tool-catalog>" not in _text()
    monkeypatch.setattr(cp, "tool_progressive_disclosure", lambda: True)
    assert "<tool-catalog>" in _text()


def test_polymarket_section_uses_namespaced_ids():
    text = _text(tool_ids=["polymarket"])
    assert "polymarket_place_limit_order" in text
    assert "pUSD" in text


def test_identity_and_memory_wording():
    text = _text()
    assert "semantic memory" not in text
    assert "parse_webpage" not in text


def test_web_access_points_at_the_remedy():
    text = _text(tool_ids=["web_fetch"])
    assert "the tool-availability note lists" in text


@pytest.mark.parametrize("stamp", ["_correspondent_session", "_correspondent_tainted"])
def test_data10_resumed_correspondent_session_without_a_chat_key_is_not_the_owner(stamp):
    """DATA-10: a resumed correspondent session binds no _chat_session_key, so no
    surface profile resolves — and an unbound session read as the owner's DM."""
    from agents.task.agent.core.construction import AgentConstructionMixin
    orch = SimpleNamespace(_chat_session_key=None, container=None, **{stamp: True})
    agent = SimpleNamespace(orchestrator=orch, logger=None)
    prof = AgentConstructionMixin._resolve_surface_profile(agent)
    assert prof and prof["correspondent"] is True
    text = _text(surface=prof)
    assert "with a correspondent, NOT your owner" in text
    assert "propose_action" not in text
    owner = SimpleNamespace(_chat_session_key=None, container=None)
    assert AgentConstructionMixin._resolve_surface_profile(
        SimpleNamespace(orchestrator=owner, logger=None)) is None
