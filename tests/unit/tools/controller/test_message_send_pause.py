"""031: the `message` tool's send path consults the ONE pause record.

Gap this closes: `perform_message_send` had no ``autonomy_control.allows(``
call at all, so an autonomous goal/cron turn calling `message` kept notifying
the owner and posting to third parties straight through `/pause pings`,
`/pause social` and even `/pause all` (for a session already past the
cancellation edge). This is the dominant real-world "notify the owner / post an
update" path.

Polarity (same as every money/social gate): an OWNER-initiated turn is never
gated — asking the agent to send IS the owner being in the loop.
"""
import asyncio
import os
import tempfile

import pytest

from core.surfaces.outbound_allowlist import OutboundAllowlist
from tools.controller.message_send import perform_message_send


class _Router:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, surface_id="telegram", media=None):
        self.sent.append((surface_id, chat_id, text))
        return True

    def capabilities(self, surface_id):
        return None


class _Ctx:
    """An execution context shaped like the run loop's."""

    def __init__(self, *, role="orchestrator", is_sub_agent=False,
                 session_id="s1", user_id="rob", metadata=None):
        self.role = role
        self.is_sub_agent = is_sub_agent
        self.session_id = session_id
        self.user_id = user_id
        self.metadata = metadata or {}


class _Controller:
    _is_sub_agent = False
    session_id = "s1"


def _autonomous_ctx():
    """A delegated/goal-run turn: role='leaf' is the cheapest honest shape
    `_is_forged_or_autonomous_turn` recognises."""
    return _Ctx(role="leaf")


def _owner_ctx():
    return _Ctx(role="orchestrator")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Pin the pause record at tmp (the conftest isolation's escape hatch)."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    return tmp_path


def _allowlist(user_id="rob", surface="telegram", target="friend"):
    tmp = tempfile.mkdtemp()
    al = OutboundAllowlist(os.path.join(tmp, "surfaces.db"))
    al.allow(user_id, surface, target)
    return al


def _send(router, *, target, ctx, allowlist=None):
    return asyncio.run(perform_message_send(
        router=router, allowlist=allowlist, owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target=target, text="hi",
        session_id="s1", container=None,
        execution_context=ctx, controller=_Controller()))


# --- the owner ping lane (`/pause pings`) ------------------------------------

def test_autonomous_owner_ping_is_refused_while_pings_paused(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("pings",), via="test")
    router = _Router()
    res = _send(router, target="owner", ctx=_autonomous_ctx())
    assert res["success"] is False
    assert "paused" in (res["error"] or "")
    assert "/resume pings" in (res["error"] or "")
    assert router.sent == [], "a paused autonomous ping must never reach the surface"


def test_pings_pause_does_not_block_a_third_party_send(home):
    """Scope precision: `/pause pings` is the owner-ping lane, not social."""
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("pings",), via="test")
    router = _Router()
    res = _send(router, target="friend", ctx=_autonomous_ctx(),
                allowlist=_allowlist())
    assert res["success"] is True, res
    assert router.sent == [("telegram", "@friend", "hi")]  # normalized handle


# --- the outward post lane (`/pause social`) ---------------------------------

def test_autonomous_third_party_send_is_refused_while_social_paused(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("social",), via="test")
    router = _Router()
    res = _send(router, target="friend", ctx=_autonomous_ctx(),
                allowlist=_allowlist())
    assert res["success"] is False
    assert "/resume social" in (res["error"] or "")
    assert router.sent == []


def test_social_pause_does_not_block_an_owner_ping(home):
    from core import autonomy_control as ac
    ac.pause(str(home), scopes=("social",), via="test")
    router = _Router()
    res = _send(router, target="owner", ctx=_autonomous_ctx())
    assert res["success"] is True, res
    assert router.sent == [("telegram", "999", "hi")]


# --- a full pause covers both ------------------------------------------------

@pytest.mark.parametrize("target,allowed", [("owner", False), ("friend", True)])
def test_full_pause_refuses_every_autonomous_send(home, target, allowed):
    from core import autonomy_control as ac
    ac.pause(str(home), via="test")
    router = _Router()
    res = _send(router, target=target, ctx=_autonomous_ctx(),
                allowlist=_allowlist() if allowed else None)
    assert res["success"] is False
    assert "paused (all)" in (res["error"] or "")
    assert router.sent == []


# --- polarity: the owner's own turn is NEVER gated ---------------------------

def test_owner_turn_still_sends_while_everything_is_paused(home):
    from core import autonomy_control as ac
    ac.pause(str(home), via="test")
    router = _Router()
    res = _send(router, target="owner", ctx=_owner_ctx())
    assert res["success"] is True, res
    assert router.sent == [("telegram", "999", "hi")]


def test_context_less_call_is_not_gated(home):
    """A programmatic/CLI call with no execution_context keeps today's behaviour
    (mirrors TwitterTool._pause_block's owner-direct escape hatch)."""
    from core import autonomy_control as ac
    ac.pause(str(home), via="test")
    router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=None, owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="owner", text="hi",
        session_id="s1", container=None))
    assert res["success"] is True, res
    assert router.sent == [("telegram", "999", "hi")]


def test_unpaused_autonomous_send_is_unchanged(home):
    router = _Router()
    res = _send(router, target="owner", ctx=_autonomous_ctx())
    assert res["success"] is True, res
    assert router.sent == [("telegram", "999", "hi")]
