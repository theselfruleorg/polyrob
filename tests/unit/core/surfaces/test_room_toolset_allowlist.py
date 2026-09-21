"""D60: the room toolset is an ALLOWLIST, not a deny-list with good intentions.

``room_tool_ids``'s docstring promised that ``GROUP_TURN_TOOLS`` "can narrow the
room toolset, never widen it past the audience bound". The derivation behind
that promise was the capability table — money / exec / delegate-blocked /
high-impact — and ``filesystem`` carries none of those bits. So
``GROUP_TURN_TOOLS=filesystem`` walked straight through the gate and gave a room
full of strangers read AND write access to the owner tenant's workspace.

The fix is a closed set: a tool must be NAMED room-safe to be reachable from a
public room, so a tool added to the tree next month is refused by default.
"""
import pytest

from core.surfaces.room_policy import (
    DEFAULT_ROOM_TOOLS, ROOM_ALLOWED_TOOL_IDS, ROOM_FORBIDDEN_TOOL_IDS,
    room_tool_ids,
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("GROUP_TURN_TOOLS", raising=False)


def test_the_default_is_unchanged():
    assert room_tool_ids() == list(DEFAULT_ROOM_TOOLS)


@pytest.mark.parametrize("tool_id", sorted(ROOM_FORBIDDEN_TOOL_IDS))
def test_every_audience_forbidden_tool_is_dropped(tool_id, monkeypatch):
    """The host, the owner's files, his browser session, his mail, his MCP
    credentials, his deployed apps — none of them belong to a room's audience,
    whatever the capability table says about them."""
    monkeypatch.setenv("GROUP_TURN_TOOLS", f"{tool_id},web_fetch")
    assert room_tool_ids() == ["web_fetch"]


def test_filesystem_by_name_because_it_is_the_one_that_got_through(monkeypatch):
    """Named on its own: it carries no capability bit, so only the explicit
    list stops it, and a future refactor of the capability table must not
    quietly re-open it."""
    monkeypatch.setenv("GROUP_TURN_TOOLS", "filesystem")
    assert room_tool_ids() == []
    assert "filesystem" in ROOM_FORBIDDEN_TOOL_IDS


def test_a_tool_outside_the_allowlist_is_dropped_even_when_harmless(monkeypatch):
    """The closed-set half. A tool nobody classified is refused rather than
    admitted, because "nobody thought about it" is not a safety argument."""
    monkeypatch.setenv("GROUP_TURN_TOOLS", "knowledge,task")
    assert room_tool_ids() == ["task"]
    assert "knowledge" not in ROOM_ALLOWED_TOOL_IDS


def test_the_env_can_only_narrow(monkeypatch):
    monkeypatch.setenv("GROUP_TURN_TOOLS", "web_fetch")
    kept = room_tool_ids()
    assert set(kept) <= set(DEFAULT_ROOM_TOOLS)
    assert kept == ["web_fetch"]


def test_an_empty_toolset_stays_empty_and_is_not_floored_back(monkeypatch):
    """An operator's deliberate lockdown must never be silently undone."""
    monkeypatch.setenv("GROUP_TURN_TOOLS", "")
    assert room_tool_ids() == []


def test_the_allowlist_and_the_forbidden_set_never_overlap():
    assert not (ROOM_ALLOWED_TOOL_IDS & ROOM_FORBIDDEN_TOOL_IDS)


def test_money_tools_are_forbidden_by_capability_not_by_name():
    """The explicit set is an ADDITION to the capability derivation, never a
    replacement: every money tool must still be refused without being listed."""
    from core.tool_capabilities import ids_with
    from core.surfaces.room_policy import _forbidden_tool_ids
    forbidden = _forbidden_tool_ids()
    for tool_id in ids_with("money"):
        assert tool_id in forbidden, f"{tool_id} is a money tool and must never be a room tool"
