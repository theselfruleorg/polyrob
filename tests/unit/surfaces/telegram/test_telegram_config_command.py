"""T10: Telegram `/config` — control-plane read/set over `core.prefs` (owner-only).

Mirrors test_owner_status_verbs.py's fake-envelope pattern (no real Telegram
objects). Covers the T10-corrections load-bearing items:

  1. dispatcher wiring: `/config` must classify as RouteKind.COMMAND (the plan
     missed this — without it the harness handler is dead code).
  2. read path: `/config` (no args) renders the SAME PREF_SCHEMA listing `/prefs`
     does (reuses `_prefs_reply` — no second PREF_SCHEMA loop).
  3. set path: a SAFE key writes immediately; a GUARDED key never writes directly
     — it queues a `propose_pref_change` proposal and points at /pending.
  4. owner gating: a non-owner gets the standard "🔒 Owner only." refusal.
"""
import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind, _COMMANDS
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.harness import _HELP, _OWNER_ADMIN_COMMANDS, act_on_inbound
from surfaces.telegram.inbound import InboundResult


class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, name):
        return None


class _Agent:
    def __init__(self, data_dir):
        self.container = _Container(data_dir)


def _cmd(command, text, user="gleb"):
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(text=text,
                             identity=Identity(user_id=user, source=src, raw_user_id="555"))
    return InboundResult(inbound=inbound, decision=RouteDecision(
        RouteKind.COMMAND, "agent:main:telegram:dm:555:" + user, command=command))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "gleb")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    return tmp_path


# --- wiring (T10-corrections item 1: must be added FIRST) --------------------

def test_config_is_a_routable_command():
    assert "/config" in _COMMANDS
    assert "/config" in _OWNER_ADMIN_COMMANDS


@pytest.mark.asyncio
async def test_dispatcher_routes_config_as_command():
    from core.surfaces.dispatcher import route_inbound

    class _FakeContainer:
        def get_service(self, name):
            return None

    msg = InboundMessage(text="/config",
                         identity=Identity(user_id="gleb",
                                          source=SessionSource("telegram", "555", "dm")))
    decision = await route_inbound(_FakeContainer(), msg)
    assert decision.kind == RouteKind.COMMAND
    assert decision.command == "/config"


@pytest.mark.asyncio
async def test_dispatcher_routes_config_set_as_command():
    from core.surfaces.dispatcher import route_inbound

    class _FakeContainer:
        def get_service(self, name):
            return None

    msg = InboundMessage(text="/config set style.verbosity terse",
                         identity=Identity(user_id="gleb",
                                          source=SessionSource("telegram", "555", "dm")))
    decision = await route_inbound(_FakeContainer(), msg)
    assert decision.kind == RouteKind.COMMAND
    assert decision.command == "/config"


# --- owner gating -------------------------------------------------------------

@pytest.mark.asyncio
async def test_config_refused_for_non_owner(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/config", "/config", user="u_stranger"))
    assert out == "🔒 Owner only."


# --- read path: no args (reuses _prefs_reply — item 3) -----------------------

@pytest.mark.asyncio
async def test_config_no_args_lists_pref_schema(env):
    from core.prefs import PREF_SCHEMA
    out = await act_on_inbound(_Agent(str(env)), _cmd("/config", "/config"))
    for key in PREF_SCHEMA:
        assert key in out


@pytest.mark.asyncio
async def test_config_list_matches_prefs_reply_verbatim(env):
    """No second PREF_SCHEMA loop — /config (no args) renders the SAME text /prefs does."""
    from surfaces.telegram.harness import _prefs_reply
    from core.instance import resolve_instance_id

    expected = _prefs_reply("gleb", str(env), resolve_instance_id())
    out = await act_on_inbound(_Agent(str(env)), _cmd("/config", "/config"))
    assert out == expected


@pytest.mark.asyncio
async def test_config_written_pref_shows_pref_source(env):
    from core.prefs import write_preference
    ok, err = write_preference(str(env), "gleb", "style.verbosity", "terse", instance_id="rob")
    assert ok, err
    out = await act_on_inbound(_Agent(str(env)), _cmd("/config", "/config"))
    assert "style.verbosity = terse (pref)" in out


# --- set path: safe key writes immediately ------------------------------------

@pytest.mark.asyncio
async def test_config_set_safe_key_writes_immediately(env):
    from core.prefs import load_preferences
    out = await act_on_inbound(
        _Agent(str(env)), _cmd("/config", "/config set style.verbosity terse"))
    assert "style.verbosity" in out
    assert "terse" in out
    assert load_preferences(str(env), "gleb", instance_id="rob").get("style.verbosity") == "terse"


@pytest.mark.asyncio
async def test_config_set_unknown_key_names_valid_groups(env):
    out = await act_on_inbound(
        _Agent(str(env)), _cmd("/config", "/config set nope.nothing value"))
    assert "unknown" in out.lower()
    # at least one real PREF_SCHEMA group is named as a hint
    assert "style" in out or "budget" in out or "approvals" in out


# --- set path: guarded key never writes directly — proposes instead ----------

@pytest.mark.asyncio
async def test_config_set_guarded_key_does_not_write(env):
    from core.prefs import load_preferences
    out = await act_on_inbound(
        _Agent(str(env)), _cmd("/config", "/config set outbound.policy open"))
    assert "guarded" in out.lower()
    assert "pending" in out.lower()
    # never written directly from a bare Telegram message
    assert load_preferences(str(env), "gleb", instance_id="rob").get("outbound.policy") is None


@pytest.mark.asyncio
async def test_config_set_guarded_key_queues_proposal(env):
    from core.prefs import list_pending_pref_changes
    await act_on_inbound(
        _Agent(str(env)), _cmd("/config", "/config set outbound.policy open"))
    items = list_pending_pref_changes("gleb", str(env), instance_id="rob")
    assert any(it["id"] == "outbound.policy" for it in items)


# --- /help mentions /config ----------------------------------------------------

@pytest.mark.asyncio
async def test_help_mentions_config(env):
    out = await act_on_inbound(_Agent(str(env)), _cmd("/help", "/help"))
    assert "/config" in out
    assert "/config" in _HELP
