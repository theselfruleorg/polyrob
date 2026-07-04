"""Task 17: Resource read-path — list + read (realpath-confined, untrusted-wrapped).

Never execute a resource file — this path is read-only.
"""
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.controller._helpers import list_skill_resources, read_skill_resource_confined
from agents.task.agent.skill_manager import SkillManager


def _skill(tmp_path):
    d = tmp_path / "demo"; (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\n# body")
    (d / "references" / "REFERENCE.md").write_text("REF CONTENT")
    (d / "scripts").mkdir(); (d / "scripts" / "run.sh").write_text("echo hi")
    return d


def test_lists_resources_excluding_skill_md(tmp_path):
    d = _skill(tmp_path)
    res = list_skill_resources(d)
    assert "references/REFERENCE.md" in res and "scripts/run.sh" in res
    assert "SKILL.md" not in res


def test_read_confined_wraps_untrusted(tmp_path):
    d = _skill(tmp_path)
    ok, content = read_skill_resource_confined(d, "references/REFERENCE.md")
    assert ok and "REF CONTENT" in content
    assert content.startswith("<untrusted_tool_result")   # UP-06 framing


def test_read_refuses_path_escape(tmp_path):
    d = _skill(tmp_path)
    (tmp_path / "secret.txt").write_text("SECRET")
    ok, msg = read_skill_resource_confined(d, "../secret.txt")
    assert not ok and "escape" in msg.lower()
    ok2, _ = read_skill_resource_confined(d, "/etc/passwd")
    assert not ok2


def test_read_refuses_oversize(tmp_path):
    d = _skill(tmp_path)
    (d / "big.txt").write_text("x" * 10)
    ok, msg = read_skill_resource_confined(d, "big.txt", max_bytes=5)
    assert not ok and "too large" in msg.lower()


# =====================================================================
# resolve_skill_dir (SkillManager) + the wired read_skill_resource action
# =====================================================================

def _fake_skill_manager(tmp_path):
    """A SkillManager whose *builtin* root is redirected to ``tmp_path`` (Task 17
    tests need to hit the ``_builtin_default_dir`` branch of ``resolve_skill_dir``,
    which is intentionally NOT governed by the constructor's ``skills_dir=`` param
    — see ``_user_dirs_root()``'s docstring on why that stays the TRUE builtin root)."""
    sm = SkillManager()
    sm._builtin_default_dir = tmp_path
    sm.skills_dir = tmp_path
    return sm


def test_resolve_skill_dir_builtin(tmp_path):
    d = _skill(tmp_path)
    sm = _fake_skill_manager(tmp_path)
    resolved = sm.resolve_skill_dir("demo")
    assert resolved is not None and resolved.resolve() == d.resolve()


def test_resolve_skill_dir_unknown_returns_none(tmp_path):
    sm = _fake_skill_manager(tmp_path)
    assert sm.resolve_skill_dir("nope-not-a-skill") is None


# =====================================================================
# Hardening: traversal guard + skills_dir-override consistency
# =====================================================================

def test_resolve_skill_dir_rejects_traversal(tmp_path):
    """A path-escaping skill_id must be refused up front, never joined into a path.

    Uses a REAL on-disk escape target (a sibling dir with its own SKILL.md) so
    this proves the guard actively refuses it, rather than passing by
    coincidence because the escaped path happens not to exist.
    """
    skills_root = tmp_path / "skills-real"
    skills_root.mkdir()
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    (escaped / "SKILL.md").write_text("---\nname: escaped\n---\n# body")
    nested = skills_root / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\nname: b\n---\n# body")

    sm = SkillManager()
    sm._builtin_default_dir = skills_root
    sm.skills_dir = skills_root

    assert sm.resolve_skill_dir("../escaped") is None  # real escape target exists on disk
    assert sm.resolve_skill_dir("a/b") is None  # real nested target exists on disk
    assert sm.resolve_skill_dir("") is None
    assert sm.resolve_skill_dir("a\\b") is None
    assert sm.resolve_skill_dir("/etc/passwd") is None


def test_resolve_skill_dir_allows_lenient_digit_leading_id(tmp_path):
    """The traversal guard must NOT reject legit lenient external ids (digit-leading),
    which the strict validate_skill_id would reject but which are valid consumed
    skills (e.g. discovered from ~/.agents/skills)."""
    d = tmp_path / "3d-modeling"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: 3d-modeling\n---\n# body")
    sm = _fake_skill_manager(tmp_path)
    resolved = sm.resolve_skill_dir("3d-modeling")
    assert resolved is not None and resolved.resolve() == d.resolve()


def test_resolve_skill_dir_honors_skills_dir_constructor_override(tmp_path):
    """Fix 1: resolve_skill_dir's builtin branch must use self.skills_dir (not the
    frozen self._builtin_default_dir), matching _load_skill_content, so a
    SkillManager(skills_dir=custom) instance (the documented test-isolation
    override) resolves the builtin branch from the override — not from the real
    installed package tree.
    """
    d = tmp_path / "override-demo"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: override-demo\n---\n# body")
    # Only the constructor arg is set here — _builtin_default_dir is left at its
    # real (frozen) value, unlike _fake_skill_manager which also mutates it.
    sm = SkillManager(skills_dir=tmp_path)
    resolved = sm.resolve_skill_dir("override-demo")
    assert resolved is not None and resolved.resolve() == d.resolve()


def _bare_controller():
    import agents.task.agent.service  # noqa: F401 -- avoid import cycle
    from tools.controller.registry.service import Registry
    from tools.controller.service import Controller

    c = object.__new__(Controller)
    c.logger = logging.getLogger("skill-resource-read-test")
    c.registry = Registry()
    c.user_id = "tenant-A"
    c.session_id = "s1"
    c.output_model = None
    c._session_skills = {}
    c._activated_skills = set()
    return c


@pytest.mark.asyncio
async def test_read_skill_resource_action_returns_wrapped_content(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "true")
    d = _skill(tmp_path)
    fake_manager = _fake_skill_manager(tmp_path)
    monkeypatch.setattr(
        "agents.task.agent.skill_manager.get_skill_manager", lambda: fake_manager
    )

    c = _bare_controller()
    c._session_skills = {"demo": SimpleNamespace(content="BODY TEXT")}
    c._register_default_actions()

    assert "read_skill_resource" in c.registry.registry.actions
    action = c.registry.registry.actions["read_skill_resource"]
    params = action.param_model(skill_id="demo", resource_path="references/REFERENCE.md")
    result = await action.function(params, execution_context=None)

    assert result.error is None
    assert "REF CONTENT" in result.extracted_content
    assert result.extracted_content.startswith("<untrusted_tool_result")


@pytest.mark.asyncio
async def test_read_skill_resource_action_refuses_unloaded_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "true")
    fake_manager = _fake_skill_manager(tmp_path)
    monkeypatch.setattr(
        "agents.task.agent.skill_manager.get_skill_manager", lambda: fake_manager
    )

    c = _bare_controller()
    c._session_skills = {}  # nothing loaded this session
    c._register_default_actions()

    action = c.registry.registry.actions["read_skill_resource"]
    params = action.param_model(skill_id="not-loaded", resource_path="references/REFERENCE.md")
    result = await action.function(params, execution_context=None)

    assert result.error is not None
    assert "not loaded" in result.error.lower()


@pytest.mark.asyncio
async def test_read_skill_resource_action_never_executes_scripts(tmp_path, monkeypatch):
    """A loaded skill's scripts/*.sh must be returned as inert TEXT, never run."""
    monkeypatch.setenv("SKILL_PROGRESSIVE_DISCLOSURE", "true")
    d = _skill(tmp_path)
    marker = tmp_path / "SHOULD_NOT_EXIST.txt"
    (d / "scripts" / "run.sh").write_text(f"#!/bin/sh\ntouch {marker}\n")
    fake_manager = _fake_skill_manager(tmp_path)
    monkeypatch.setattr(
        "agents.task.agent.skill_manager.get_skill_manager", lambda: fake_manager
    )

    c = _bare_controller()
    c._session_skills = {"demo": SimpleNamespace(content="BODY TEXT")}
    c._register_default_actions()

    action = c.registry.registry.actions["read_skill_resource"]
    params = action.param_model(skill_id="demo", resource_path="scripts/run.sh")
    result = await action.function(params, execution_context=None)

    assert result.error is None
    assert "touch" in result.extracted_content  # returned as literal text
    assert not marker.exists()  # never executed
