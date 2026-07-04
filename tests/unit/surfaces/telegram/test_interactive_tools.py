"""Interactive OWNER telegram session gets the introspection + mission toolset.

Bug (2026-07-03): an inbound telegram message created a session with the bare
SessionRequest default ['browser','filesystem','task'] — no `goal`, so when the owner
asked "review your goals" the agent had no goal_list tool, fell back to reading its
session sandbox, and hallucinated "goal database is empty / repo missing". The owner
principal's interactive session must get `goal` (+ twitter/web_fetch) so it can actually
introspect its board and act on-mission. A non-owner keeps the conservative default.
"""
import pytest

from surfaces.telegram.interactive_tools import owner_interactive_tool_ids


def test_owner_gets_goal_and_mission_tools(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    tools = owner_interactive_tool_ids("rob")
    assert tools is not None
    assert "goal" in tools          # can introspect the board (no more filesystem-guessing)
    assert "twitter" in tools       # can act on-mission (post)
    assert "web_fetch" in tools


def test_non_owner_keeps_default(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    assert owner_interactive_tool_ids("u_deadbeef") is None


def test_owner_tool_ids_env_overridable(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.setenv("INTERACTIVE_TOOL_IDS", "goal,filesystem,task")
    tools = owner_interactive_tool_ids("rob")
    assert tools == ["goal", "filesystem", "task"]
