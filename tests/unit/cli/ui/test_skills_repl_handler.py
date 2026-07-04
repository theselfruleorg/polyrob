"""Task 22: REPL `/skills [list|install|approve|remove|info]` subcommand dispatch.

The `/skills` slash command already existed (``cli/ui/commands/h_skills.py``) as a
read-only catalog listing/filter. This extends the SAME handler with a subcommand
dispatch layer that mirrors the CLI's `polyrob skill` group, wired through the
shared library functions in ``cli.commands.skill_install`` (no duplicated logic).
`install`/`approve` are gated on the local operator (`agents.task.constants.
local_mode_enabled`) — Task 23 will harden the library layer itself; this is the
REPL-side UX gate.
"""
from __future__ import annotations

import io
import asyncio

from cli.ui.commands.registry import CommandContext
from cli.ui.commands.h_skills import h_skills
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


def _plain_ctx(**overrides):
    buf = io.StringIO()
    state = overrides.pop("state", SessionState())
    renderer = PlainRenderer(state=state, stream=buf)
    ctx = CommandContext(renderer=renderer, state=state, **overrides)
    return ctx, buf


# ---------------------------------------------------------------------------
# /skills list
# ---------------------------------------------------------------------------


def test_skills_list_dispatches_to_list_all_skills(monkeypatch):
    calls = []

    def fake_list_all_skills(user_id):
        calls.append(user_id)
        return [
            {"id": "builtin-one", "scope": "builtin", "status": "active"},
            {"id": "pending-one", "scope": "user", "status": "pending"},
        ]

    monkeypatch.setattr("cli.commands.skill_install.list_all_skills", fake_list_all_skills)
    ctx, buf = _plain_ctx(user_id="local", args=["list"])
    asyncio.run(h_skills(ctx))
    out = buf.getvalue()
    assert calls == ["local"]
    assert "builtin-one" in out
    assert "pending-one" in out


def test_skills_list_is_graceful_on_backend_error(monkeypatch):
    def _boom(user_id):
        raise RuntimeError("db locked")

    monkeypatch.setattr("cli.commands.skill_install.list_all_skills", _boom)
    ctx, buf = _plain_ctx(args=["list"])
    asyncio.run(h_skills(ctx))  # must not raise
    assert "db locked" in buf.getvalue() or "Could not" in buf.getvalue()


# ---------------------------------------------------------------------------
# /skills info <id>
# ---------------------------------------------------------------------------


def test_skills_info_dispatches_to_get_skill_info(monkeypatch):
    def fake_get_skill_info(skill_id, user_id):
        assert skill_id == "widgeter"
        assert user_id == "local"
        return {"id": "widgeter", "status": "active", "description": "Does widgets."}

    monkeypatch.setattr("cli.commands.skill_install.get_skill_info", fake_get_skill_info)
    ctx, buf = _plain_ctx(user_id="local", args=["info", "widgeter"])
    asyncio.run(h_skills(ctx))
    out = buf.getvalue()
    assert "widgeter" in out
    assert "Does widgets." in out


def test_skills_info_unknown_id_is_graceful(monkeypatch):
    from cli.commands.skill_install import InstallError

    def fake_get_skill_info(skill_id, user_id):
        raise InstallError(f"skill {skill_id!r} not found")

    monkeypatch.setattr("cli.commands.skill_install.get_skill_info", fake_get_skill_info)
    ctx, buf = _plain_ctx(args=["info", "nope"])
    asyncio.run(h_skills(ctx))  # must not raise
    assert "not found" in buf.getvalue()


def test_skills_info_missing_arg_shows_usage():
    ctx, buf = _plain_ctx(args=["info"])
    asyncio.run(h_skills(ctx))
    assert "Usage" in buf.getvalue()


# ---------------------------------------------------------------------------
# /skills remove <id>
# ---------------------------------------------------------------------------


def test_skills_remove_dispatches_to_remove_skill(monkeypatch):
    calls = []

    def fake_remove_skill(skill_id, user_id):
        calls.append((skill_id, user_id))
        return True

    monkeypatch.setattr("cli.commands.skill_install.remove_skill", fake_remove_skill)
    ctx, buf = _plain_ctx(user_id="local", args=["remove", "oldskill"])
    asyncio.run(h_skills(ctx))
    assert calls == [("oldskill", "local")]
    assert "removed" in buf.getvalue().lower() or "archived" in buf.getvalue().lower()


def test_skills_remove_not_found_is_graceful(monkeypatch):
    monkeypatch.setattr("cli.commands.skill_install.remove_skill", lambda s, u: False)
    ctx, buf = _plain_ctx(args=["remove", "nope"])
    asyncio.run(h_skills(ctx))
    assert "not" in buf.getvalue().lower()


# ---------------------------------------------------------------------------
# /skills install <spec> — requires the local operator
# ---------------------------------------------------------------------------


def test_skills_install_refuses_when_not_local(monkeypatch):
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("dispatch_install must not be called when not local")

    monkeypatch.setattr("cli.commands.skill_install.dispatch_install", _boom)
    ctx, buf = _plain_ctx(args=["install", "owner/repo"])
    asyncio.run(h_skills(ctx))
    out = buf.getvalue().lower()
    assert "local" in out or "refus" in out


def test_skills_install_dispatches_when_local(monkeypatch):
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: True)
    calls = {}

    def fake_dispatch_install(spec, *, user_id, trust="prompt", ref=None):
        calls["spec"] = spec
        calls["user_id"] = user_id
        from cli.commands.skill_install import InstallResult
        return InstallResult(name="fromrepo", staged_path="/tmp/x", approved=False, source=f"git:{spec}")

    monkeypatch.setattr("cli.commands.skill_install.dispatch_install", fake_dispatch_install)
    ctx, buf = _plain_ctx(user_id="local", args=["install", "owner/repo"])
    asyncio.run(h_skills(ctx))
    assert calls["spec"] == "owner/repo"
    assert calls["user_id"] == "local"
    assert "fromrepo" in buf.getvalue()


# ---------------------------------------------------------------------------
# /skills approve <id> — requires the local operator
# ---------------------------------------------------------------------------


def test_skills_approve_refuses_when_not_local(monkeypatch):
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: False)

    def _boom(*a, **k):
        raise AssertionError("_approve must not be called when not local")

    monkeypatch.setattr("cli.commands.skill_install._approve", _boom)
    ctx, buf = _plain_ctx(args=["approve", "widgeter"])
    asyncio.run(h_skills(ctx))
    out = buf.getvalue().lower()
    assert "local" in out or "refus" in out


def test_skills_approve_dispatches_when_local(monkeypatch):
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: True)
    calls = []

    def fake_approve(name, *, user_id, source="local", resolved_sha=None):
        calls.append((name, user_id))

    monkeypatch.setattr("cli.commands.skill_install._approve", fake_approve)
    ctx, buf = _plain_ctx(user_id="local", args=["approve", "widgeter"])
    asyncio.run(h_skills(ctx))
    assert calls == [("widgeter", "local")]
    assert "approved" in buf.getvalue().lower()


# ---------------------------------------------------------------------------
# Backward compat: bare `/skills <query>` still filters the catalog (unchanged)
# ---------------------------------------------------------------------------


class _FakeSkill:
    def __init__(self, skill_id: str, description: str = "") -> None:
        self.skill_id = skill_id
        self.description = description


class _FakeManager:
    def __init__(self, skills):
        self._skills = skills

    def get_catalog_skills(self, user_id=None, max_skills=20):
        return list(self._skills)


def test_bare_query_still_filters_catalog_not_treated_as_subcommand(monkeypatch):
    manager = _FakeManager([_FakeSkill("web-research", "Research the web")])
    monkeypatch.setattr("agents.task.agent.skill_manager.get_skill_manager", lambda: manager)
    ctx, buf = _plain_ctx(args=["web"])
    asyncio.run(h_skills(ctx))
    assert "web-research" in buf.getvalue()
