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


def test_banner_folds_default_instance_equal_to_framework():
    # Fresh install: instance id == framework name ("polyrob") — showing
    # "polyrob · instance polyrob" is noise, so the instance part is folded.
    text = banner_plain(
        version="1.0.0", model="m", provider="p", tool_ids=[], session_id="s",
        framework="polyrob", instance_id="polyrob",
    )
    third = text.splitlines()[2]
    assert "polyrob" in third
    assert "instance" not in third


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


# ---------------------------------------------------------------------------
# /session owner line — the ONE owner label every seat shares (043 residue N2)
# ---------------------------------------------------------------------------

_OWNER_KEYS = ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
               "SURFACE_SUPER_ADMIN_USER_IDS", "POLYROB_LOCAL_OWNER")


def _unbind(monkeypatch):
    for key in _OWNER_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_session_owner_line_says_unpaired_when_unbound(monkeypatch):
    """⚠️ The REPL and `polyrob doctor` are two owner seats on ONE box; they must
    not disagree about whether an owner is bound.

    This line read `resolve_owner_principal() or "unbound (local owner)"`. When
    the unbound principal became the owner TENANT, the `or` stopped firing and the
    line printed a bare `local` — which reads exactly like an owner explicitly
    bound to that name — while `polyrob doctor` said `(unpaired)` one command
    away. Both seats now render `core.instance.owner_label`.
    """
    from core.instance import UNPAIRED_OWNER_LABEL
    _unbind(monkeypatch)
    ctx, buf = _session_ctx(state=SessionState(), session_id="s1", user_id="local")
    build_default_registry().lookup("session").handler(ctx)
    out = buf.getvalue()
    assert UNPAIRED_OWNER_LABEL in out
    assert "POLYROB_OWNER_USER_ID" in out        # the remedy is named
    assert "owner: local" not in out             # never a bare tenant name


def test_session_owner_line_names_a_bound_owner(monkeypatch):
    _unbind(monkeypatch)
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    ctx, buf = _session_ctx(state=SessionState(), session_id="s1", user_id="local")
    build_default_registry().lookup("session").handler(ctx)
    out = buf.getvalue()
    assert "alice" in out
    assert "unpaired" not in out


def test_session_owner_line_marks_an_owner_that_is_the_instance(monkeypatch):
    """Prod's shape: owner `rob` bound on instance `rob` — named, and marked so it
    does not read as a second, distinct human owner."""
    _unbind(monkeypatch)
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    ctx, buf = _session_ctx(state=SessionState(), session_id="s1", user_id="local")
    build_default_registry().lookup("session").handler(ctx)
    out = buf.getvalue()
    assert "this instance's own tenant" in out
    assert "unpaired" not in out
