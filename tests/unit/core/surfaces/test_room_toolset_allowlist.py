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


# --- 101 Phase 2: what the owner granted one room ---------------------------
from types import SimpleNamespace  # noqa: E402

from core.surfaces.group_admin import _grant_refusal as validate_grant  # noqa: E402
from core.surfaces.room_grants import (ADMIN_BUNDLES, live_admin_bundles,  # noqa: E402
                                       member_grantable)


def _policy(**kw):
    return SimpleNamespace(room_tools=kw.get("room_tools", ()),
                           admin_tools=kw.get("admin_tools", ()))


def test_a_grant_adds_to_the_room_toolset(monkeypatch):
    monkeypatch.setenv("GROUP_TURN_TOOLS", "task")
    assert room_tool_ids(policy=_policy(room_tools=("web_fetch",))) == ["task", "web_fetch"]


def test_a_hand_edited_policy_never_widens():
    for tool in ("filesystem", "browser", "http_request", "defi_trade", "shell", "mcp", "nope"):
        assert tool not in room_tool_ids(policy=_policy(room_tools=(tool,)))
    assert live_admin_bundles(_policy(admin_tools=("moderation", "root", "exec"))) == ("moderation",)


def test_a_lockdown_still_gives_no_base_tools(monkeypatch):
    monkeypatch.setenv("GROUP_TURN_TOOLS", "")
    assert room_tool_ids() == []
    assert room_tool_ids(policy=_policy()) == []


def test_a_row_alone_cannot_make_a_dangerous_tool_member_wide(monkeypatch):
    """R11: a pack writes its own row, so the capability check runs at read."""
    import core.tool_capabilities as tc
    for caps in (("money",), ("exec",), ("high_impact",), ("writes_network",),
                 ("shared_identity",), ("delegate_blocked",)):
        monkeypatch.setitem(tc.TOOL_CAPABILITIES, "evil_tool",
                            tc.ToolRow(set(caps), room_grantable=True))
        assert "evil_tool" not in member_grantable(), caps
    monkeypatch.setitem(tc.TOOL_CAPABILITIES, "good_tool", tc.ToolRow(room_grantable=True))
    assert "good_tool" in member_grantable()


def test_a_write_names_the_closed_set():
    assert "filesystem" in validate_grant("chat.room_tools", "web_fetch,filesystem")
    assert validate_grant("chat.room_tools", "web_fetch") is None
    assert "root" in validate_grant("chat.admin_tools", "moderation,root")
    assert validate_grant("chat.admin_tools", "moderation,files") is None
    assert validate_grant("chat.mode", "active") is None
    assert set(ADMIN_BUNDLES) == {"moderation", "files", "http", "browser"}
