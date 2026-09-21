"""Tests for the ten REPL parity verbs (043 A23).

The verbs live one-per-module under ``cli/ui/commands/h_*.py`` and are wired into
``build_default_registry`` through the ``h_a23`` aggregator (``handlers.py`` is at
its size ratchet). These tests pin: every verb is registered with a real group +
``help_long`` so ``/help <verb>`` answers; ``/steer`` (new on every seat) routes a
guidance message into the bound session; and the light read-only verbs render an
honest string rather than raising.

Mirrors ``tests/unit/cli/ui/test_h_cron.py`` for CommandContext + output capture.
"""
from __future__ import annotations

import asyncio
import io
import types

from cli.ui.commands.handlers import build_default_registry
from cli.ui.commands.registry import GROUP_ORDER, CommandContext
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState

# The ten A23 verbs. `/inbox` and `/missed` already existed — they are NOT here.
A23_VERBS = ("steer", "goal", "reject", "wallet", "trade", "bridge",
             "dev", "mode", "prefs", "files")


def _ctx(buf=None, **overrides):
    """A CommandContext with a PlainRenderer writing to a StringIO."""
    buf = buf if buf is not None else io.StringIO()
    state = overrides.pop("state", SessionState())
    renderer = PlainRenderer(state=state, stream=buf)
    ctx = CommandContext(renderer=renderer, state=state, **overrides)
    return ctx, buf


def _fake_container(data_dir):
    """A minimal DI-container stub whose config carries a tmp data_dir, so the
    verbs read/write a tmp home rather than the developer's real one."""
    return types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(data_dir)))


# ---------------------------------------------------------------------------
# Registration + /help
# ---------------------------------------------------------------------------

def test_all_ten_verbs_registered_with_group_and_help():
    reg = build_default_registry()
    for verb in A23_VERBS:
        cmd = reg.lookup(verb)
        assert cmd is not None, f"/{verb} not registered"
        assert cmd.group in GROUP_ORDER, f"/{verb} group {cmd.group!r} not in GROUP_ORDER"
        assert cmd.help_long, f"/{verb} has no help_long (/help {verb} would not answer)"
        assert cmd.help, f"/{verb} has no one-line help"


def test_help_answers_for_each_verb():
    """`/help <verb>` renders the verb line + its help_long, never 'Unknown'."""
    reg = build_default_registry()
    for verb in A23_VERBS:
        ctx, buf = _ctx(registry=reg)
        asyncio.run(reg.dispatch(f"/help {verb}", ctx))
        out = buf.getvalue()
        assert f"/{verb}" in out, f"/help {verb} did not name the verb: {out!r}"
        assert "Unknown command" not in out, f"/help {verb} reported unknown: {out!r}"


# ---------------------------------------------------------------------------
# /steer — the new verb: it routes into the bound session's HITL ingress
# ---------------------------------------------------------------------------

class _FakeOrch:
    def __init__(self):
        self.calls = []

    async def submit_user_message(self, agent_id, text, kind="comment", metadata=None):
        self.calls.append((agent_id, text, kind, metadata))


def test_steer_injects_guidance_into_bound_session():
    reg = build_default_registry()
    orch = _FakeOrch()
    ctx, buf = _ctx(registry=reg, orchestrator=orch)
    asyncio.run(reg.dispatch("/steer prefer the cheaper provider", ctx))
    assert orch.calls, "submit_user_message was never called"
    agent_id, text, kind, _meta = orch.calls[0]
    assert agent_id is None                      # route to the session's agent
    assert text == "prefer the cheaper provider"
    assert kind == "comment"                     # trusted-human intake kind
    assert "Steered" in buf.getvalue()


def test_steer_without_a_bound_session_is_honest():
    reg = build_default_registry()
    ctx, buf = _ctx(registry=reg, orchestrator=None)
    asyncio.run(reg.dispatch("/steer do the thing", ctx))
    out = buf.getvalue()
    assert "No live session" in out
    # No crash, no false "Steered".
    assert "Steered" not in out


def test_steer_requires_text():
    reg = build_default_registry()
    orch = _FakeOrch()
    ctx, buf = _ctx(registry=reg, orchestrator=orch)
    asyncio.run(reg.dispatch("/steer", ctx))
    assert "Usage:" in buf.getvalue()
    assert not orch.calls  # empty steer never reaches the session


# ---------------------------------------------------------------------------
# Light read-only verbs render an honest string on an empty tmp home
# ---------------------------------------------------------------------------

def test_reject_nothing_pending(tmp_path, monkeypatch):
    """C47: the ONE empty grammar (``candy.empty``) — the same sentence
    ``/approve`` renders over the same union."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    reg = build_default_registry()
    ctx, buf = _ctx(registry=reg, container=_fake_container(tmp_path), user_id="local")
    asyncio.run(reg.dispatch("/reject", ctx))
    assert "no items waiting on you" in buf.getvalue()


def test_goal_bare_shows_usage(tmp_path):
    reg = build_default_registry()
    ctx, buf = _ctx(registry=reg, container=_fake_container(tmp_path), user_id="local")
    asyncio.run(reg.dispatch("/goal", ctx))
    out = buf.getvalue()
    assert "Usage:" in out or "/goals" in out  # bare /goal points at the board


def test_bridge_bare_shows_usage(tmp_path):
    reg = build_default_registry()
    ctx, buf = _ctx(registry=reg, container=_fake_container(tmp_path), user_id="local")
    asyncio.run(reg.dispatch("/bridge", ctx))
    assert "Usage:" in buf.getvalue()


def test_mode_renders_a_posture_card():
    reg = build_default_registry()
    ctx, buf = _ctx(registry=reg)
    asyncio.run(reg.dispatch("/mode", ctx))
    assert buf.getvalue().strip()  # a non-empty posture card, never a bare crash


def test_prefs_on_empty_home_is_honest(tmp_path):
    reg = build_default_registry()
    ctx, buf = _ctx(registry=reg, container=_fake_container(tmp_path), user_id="local")
    asyncio.run(reg.dispatch("/prefs", ctx))
    out = buf.getvalue()
    assert out.strip()
    # An empty home has nothing SET — the summary says so rather than lying.
    assert "No preferences set" in out or "preferences" in out.lower()


# ---------------------------------------------------------------------------
# R7 parity: the surface matrix rows for these verbs resolve in the registry
# ---------------------------------------------------------------------------

def test_surface_parity_rows_resolve():
    """The CAPABILITY_MATRIX rows this task filled in must resolve — the same
    guard R7 enforces, pinned here so a future drop of any of these REPL verbs
    fails loudly instead of quietly reopening a parity gap."""
    reg = build_default_registry()
    # (capability, repl slash) pairs updated for A23.
    expected = {
        "wallet", "trade", "bridge", "goal", "reject", "files", "dev",
    }
    for slash in expected:
        assert reg.lookup(slash) is not None, f"REPL /{slash} missing (A23 parity)"
