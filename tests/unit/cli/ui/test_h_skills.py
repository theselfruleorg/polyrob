"""Tests for the ``/skills`` slash-command handler (cli/ui/commands/h_skills.py)."""

from __future__ import annotations

import asyncio
import io

from cli.ui.commands.registry import CommandContext
from cli.ui.commands.h_skills import h_skills
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(ctx):
    """Drive the now-async ``h_skills`` handler from a sync test."""
    return asyncio.run(h_skills(ctx))


def _plain_ctx(**overrides):
    """Build a CommandContext with a PlainRenderer writing to a StringIO."""
    buf = io.StringIO()
    state = overrides.pop("state", SessionState())
    renderer = PlainRenderer(state=state, stream=buf)
    ctx = CommandContext(renderer=renderer, state=state, **overrides)
    return ctx, buf


class _FakeSkill:
    def __init__(self, skill_id: str, description: str = "") -> None:
        self.skill_id = skill_id
        self.description = description


class _FakeManager:
    def __init__(self, skills):
        self._skills = skills
        self.calls = []

    def get_catalog_skills(self, user_id=None, max_skills=20):
        self.calls.append((user_id, max_skills))
        return list(self._skills)


def _patch_manager(monkeypatch, manager):
    monkeypatch.setattr(
        "agents.task.agent.skill_manager.get_skill_manager",
        lambda: manager,
    )


# ---------------------------------------------------------------------------
# No-args: list every skill
# ---------------------------------------------------------------------------


def test_no_args_lists_skills(monkeypatch):
    manager = _FakeManager([
        _FakeSkill("web-research", "Research topics on the web and cite sources"),
        _FakeSkill("data-cleaner", "Clean and normalize messy datasets"),
    ])
    _patch_manager(monkeypatch, manager)
    ctx, buf = _plain_ctx(user_id="local")
    _run(ctx)
    out = buf.getvalue()
    assert "web-research" in out
    assert "data-cleaner" in out
    assert "Research topics on the web" in out
    assert "2 skill(s) available" in out
    # user_id was threaded through to the backend.
    assert manager.calls and manager.calls[0][0] == "local"


def test_no_args_sorted_by_id(monkeypatch):
    manager = _FakeManager([
        _FakeSkill("zebra", "last"),
        _FakeSkill("alpha", "first"),
    ])
    _patch_manager(monkeypatch, manager)
    ctx, buf = _plain_ctx()
    _run(ctx)
    out = buf.getvalue()
    assert out.index("alpha") < out.index("zebra")


def test_long_description_truncated(monkeypatch):
    long_desc = "x" * 200
    manager = _FakeManager([_FakeSkill("big", long_desc)])
    _patch_manager(monkeypatch, manager)
    ctx, buf = _plain_ctx()
    _run(ctx)
    out = buf.getvalue()
    assert "…" in out
    assert ("x" * 200) not in out


# ---------------------------------------------------------------------------
# Query filtering
# ---------------------------------------------------------------------------


def test_query_filters_by_id(monkeypatch):
    manager = _FakeManager([
        _FakeSkill("web-research", "Research on the web"),
        _FakeSkill("data-cleaner", "Clean datasets"),
    ])
    _patch_manager(monkeypatch, manager)
    ctx, buf = _plain_ctx(args=["web"])
    _run(ctx)
    out = buf.getvalue()
    assert "web-research" in out
    assert "data-cleaner" not in out


def test_query_filters_by_description_case_insensitive(monkeypatch):
    manager = _FakeManager([
        _FakeSkill("web-research", "Research on the web"),
        _FakeSkill("data-cleaner", "Clean DATASETS quickly"),
    ])
    _patch_manager(monkeypatch, manager)
    ctx, buf = _plain_ctx(args=["dataset"])  # lowercase query vs uppercase desc
    _run(ctx)
    out = buf.getvalue()
    assert "data-cleaner" in out
    assert "web-research" not in out


def test_query_no_match_is_graceful(monkeypatch):
    manager = _FakeManager([_FakeSkill("web-research", "Research on the web")])
    _patch_manager(monkeypatch, manager)
    ctx, buf = _plain_ctx(args=["nonexistentquery"])
    _run(ctx)
    out = buf.getvalue()
    assert "no skills match" in out
    assert "nonexistentquery" in out


# ---------------------------------------------------------------------------
# Empty / unavailable
# ---------------------------------------------------------------------------


def test_empty_catalog_is_graceful(monkeypatch):
    manager = _FakeManager([])
    _patch_manager(monkeypatch, manager)
    ctx, buf = _plain_ctx()
    _run(ctx)
    assert "no skills available" in buf.getvalue()


def test_manager_unavailable_is_graceful(monkeypatch):
    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "agents.task.agent.skill_manager.get_skill_manager", _boom
    )
    ctx, buf = _plain_ctx()
    _run(ctx)
    out = buf.getvalue()
    assert "Could not load skills" in out
    assert "boom" in out


def test_never_raises_on_backend_error(monkeypatch):
    class _BadManager:
        def get_catalog_skills(self, user_id=None, max_skills=20):
            raise ValueError("db locked")

    _patch_manager(monkeypatch, _BadManager())
    ctx, buf = _plain_ctx()
    # Must not raise.
    _run(ctx)
    assert "Could not load skills" in buf.getvalue()


# ---------------------------------------------------------------------------
# install/approve: blocking pipeline is offloaded off the event-loop thread
# ---------------------------------------------------------------------------


class _InstallRes:
    def __init__(self, name: str, approved: bool) -> None:
        self.name = name
        self.approved = approved


def _spy_to_thread(monkeypatch):
    """Wrap ``asyncio.to_thread`` (as seen by the handler) so a test can assert
    the blocking call was pushed off the event loop, while still executing it."""
    real_to_thread = asyncio.to_thread
    record = {"count": 0, "fn": None}

    async def spy(fn, *a, **k):
        record["count"] += 1
        record["fn"] = fn
        return await real_to_thread(fn, *a, **k)

    monkeypatch.setattr("cli.ui.commands.h_skills.asyncio.to_thread", spy)
    return record


def test_install_is_offloaded_to_thread(monkeypatch):
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: True)

    called = {}

    def fake_dispatch(spec, *, user_id, trust, ref):
        called["args"] = (spec, user_id, trust, ref)
        return _InstallRes("cool-skill", approved=True)

    monkeypatch.setattr(
        "cli.commands.skill_install.dispatch_install", fake_dispatch
    )
    record = _spy_to_thread(monkeypatch)

    ctx, buf = _plain_ctx(args=["install", "owner/repo"], user_id="local")
    _run(ctx)

    out = buf.getvalue()
    # The blocking dispatch_install ran via asyncio.to_thread, not inline.
    assert record["count"] == 1
    assert record["fn"] is fake_dispatch
    assert called["args"] == ("owner/repo", "local", "prompt", None)
    assert "cool-skill" in out


def test_install_quarantine_message_offloaded(monkeypatch):
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: True)
    monkeypatch.setattr(
        "cli.commands.skill_install.dispatch_install",
        lambda spec, *, user_id, trust, ref: _InstallRes("staged", approved=False),
    )
    record = _spy_to_thread(monkeypatch)

    ctx, buf = _plain_ctx(args=["install", "https://x/SKILL.md"], user_id="local")
    _run(ctx)

    out = buf.getvalue()
    assert record["count"] == 1
    assert "quarantine" in out
    assert "approve staged" in out


def test_install_refused_when_not_local_operator(monkeypatch):
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: False)
    record = _spy_to_thread(monkeypatch)

    ctx, buf = _plain_ctx(args=["install", "owner/repo"], user_id="local")
    _run(ctx)

    out = buf.getvalue()
    # Gate fires before any offload — no blocking work scheduled.
    assert record["count"] == 0
    assert "Refused" in out
    assert "install" in out


def test_approve_is_offloaded_to_thread(monkeypatch):
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: True)

    called = {}

    def fake_approve(name, *, user_id, source):
        called["args"] = (name, user_id, source)

    monkeypatch.setattr("cli.commands.skill_install._approve", fake_approve)
    record = _spy_to_thread(monkeypatch)

    ctx, buf = _plain_ctx(args=["approve", "staged"], user_id="local")
    _run(ctx)

    out = buf.getvalue()
    assert record["count"] == 1
    assert record["fn"] is fake_approve
    assert called["args"] == ("staged", "local", "local")
    assert "Approved" in out
