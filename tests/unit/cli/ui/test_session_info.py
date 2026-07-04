"""P1: prominent session info + polyrob/rob framework-instance naming.

Two surfaces:
- the startup banner gains an OPT-IN third line (framework · instance · user ·
  memory · autonomy) — inert (2 lines) when the new kwargs aren't supplied, so
  the existing banner tests stay green.
- a ``/session`` (alias ``/info``) command renders the full identity snapshot.
"""

from __future__ import annotations

import io

from cli.ui.banner import banner_plain
from cli.ui.commands import (
    CommandContext,
    build_default_registry,
)
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


# ---------------------------------------------------------------------------
# Banner: opt-in session-info line
# ---------------------------------------------------------------------------


def test_banner_without_session_info_is_two_lines():
    """Default call (no new kwargs) stays the quiet two-liner — unchanged."""
    text = banner_plain(
        version="1.0.0", model="m", provider="p", tool_ids=["task"], session_id="s",
    )
    assert len(text.splitlines()) == 2


def test_banner_session_info_line_surfaces_polyrob_and_instance():
    text = banner_plain(
        version="1.0.0",
        model="m",
        provider="p",
        tool_ids=["task"],
        session_id="s",
        framework="polyrob",
        instance_id="rob",
        user_id="local",
        memory_backend="sqlite",
        autonomy_on=True,
    )
    lines = text.splitlines()
    assert len(lines) == 3
    third = lines[2]
    assert "polyrob" in third
    assert "instance rob" in third
    assert "user local" in third
    assert "memory sqlite" in third
    assert "autonomy on" in third


def test_banner_session_info_custom_instance():
    text = banner_plain(
        version="1.0.0", model="m", provider="p", tool_ids=[], session_id="s",
        framework="polyrob", instance_id="acme",
    )
    assert "instance acme" in text.splitlines()[2]


# ---------------------------------------------------------------------------
# /session command
# ---------------------------------------------------------------------------


def _session_ctx(**overrides):
    buf = io.StringIO()
    state = overrides.pop("state", SessionState())
    renderer = PlainRenderer(state=state, stream=buf)
    ctx = CommandContext(renderer=renderer, state=state, **overrides)
    return ctx, buf


def test_session_command_registered_with_info_alias():
    reg = build_default_registry()
    cmd = reg.lookup("session")
    assert cmd is not None
    assert reg.lookup("info") is cmd  # alias


def test_session_command_renders_identity_snapshot():
    state = SessionState()
    state.model = "glm-5.2"
    state.provider = "openrouter"
    ctx, buf = _session_ctx(state=state, session_id="ab7ab48c9999", user_id="local")
    cmd = build_default_registry().lookup("session")
    cmd.handler(ctx)
    out = buf.getvalue()
    assert "polyrob" in out          # framework
    assert "rob" in out              # default instance
    assert "local" in out            # user
    assert "ab7ab48c" in out         # session id
    assert "glm-5.2" in out          # model
    assert "openrouter" in out       # provider


def test_session_command_is_fail_open_with_empty_context():
    """A bare context (no state) must not raise."""
    ctx, buf = _session_ctx(state=None)
    cmd = build_default_registry().lookup("session")
    cmd.handler(ctx)  # must not raise
