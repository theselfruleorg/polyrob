"""Behavioral regressions from the agent-intelligence revalidation."""
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import sys

import pytest

BODY = "# Audit procedure\n\nRead the supplied fixture and summarize its contents.\n"


@pytest.fixture
def skills(tmp_path, monkeypatch):
    from agents.task.agent.skill_manager import SkillManager
    manager = SkillManager(skills_dir=tmp_path / "skills")
    monkeypatch.setattr(manager, "_record_provenance", lambda *a, **k: None)
    return manager


@pytest.mark.parametrize("uid", ["x/../../escaped", "../u2", "/tmp/escaped", "u1\\..\\u2", ""])
def test_skill_tenant_ids_rejected_before_io(skills, uid, tmp_path):
    assert not skills.create_skill("audit", BODY, user_id=uid).ok
    assert not skills.patch_skill("audit", user_id=uid, old_string="a", new_string="b").ok
    assert not skills.delete_skill("audit", user_id=uid)
    assert not skills.promote_pending_skill("audit", user_id=uid).ok
    assert not skills.reject_pending_skill("audit", user_id=uid)
    assert skills.list_pending_skills(uid) == []
    assert skills._load_user_rules(uid)[0] == {}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("component", ["user_u1", "user_u1/.pending", "user_u1/.pending/audit"])
def test_skill_writer_refuses_symlink_components(skills, tmp_path, component):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = skills.skills_dir / component
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    assert not skills.create_skill("audit", BODY, user_id="u1", pending=True).ok
    assert not list(outside.iterdir())
    assert skills.list_pending_skills("u1") == []


def test_skill_reads_refuse_file_links(skills, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text(BODY)
    active = skills.skills_dir / "user_u1" / "audit" / "SKILL.md"
    active.parent.mkdir(parents=True)
    active.symlink_to(outside)
    assert skills._find_skill_file("u1", "audit") is None
    assert skills._load_skill_content("audit", "u1") == ""


def test_scanner_unavailable_cannot_publish(skills, monkeypatch):
    monkeypatch.setitem(sys.modules, "modules.memory.task.threat_scan", None)
    result = skills.create_skill("audit", BODY, user_id="u1", created_by="user", pending=False)
    assert not result.ok and "scanner unavailable" in " ".join(result.errors)


def test_server_context_does_not_follow_workspace_file_link(tmp_path):
    from agents.task.agent.core.project_context import build_project_context_message
    outside = tmp_path / "outside.txt"
    outside.write_text("# Context\n\nOUTSIDE_MARKER\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").symlink_to(outside)
    result = build_project_context_message(
        local=False, autoload=False, server_mode=True,
        cwd=str(tmp_path), workspace_dir=str(workspace))
    assert result is None


def test_project_context_refuses_unavailable_scanner_and_oversized_file(tmp_path, monkeypatch):
    from agents.task.agent.core.project_context import load_project_context
    (tmp_path / "AGENTS.md").write_text("x" * (4 * 1024 * 1024 + 1))
    assert load_project_context(tmp_path, confine_to_root=True) is None
    (tmp_path / "AGENTS.md").write_text("# Context\n\nSummarize the supplied fixture.\n")
    monkeypatch.setitem(sys.modules, "modules.memory.task.threat_scan", None)
    assert load_project_context(tmp_path, confine_to_root=True) is None


def test_model_recovery_uses_complete_adoption():
    from agents.task.agent.core.error_recovery import ErrorRecoveryMixin
    from agents.task.agent.core.model_swap import ModelSwapMixin

    class Agent(ModelSwapMixin):
        pass

    old = SimpleNamespace(model_name="gpt-4o")
    new = SimpleNamespace(model_name="claude-sonnet-4-5", provider="anthropic")
    agent = Agent()
    agent.llm, agent.model_name, agent.llm_provider = old, old.model_name, "openai"
    agent.message_manager = SimpleNamespace(llm=old, set_runtime_identity=Mock())
    agent.logger = logging.getLogger("fallback-regression")
    agent.state = SimpleNamespace(llm_providers_failed=[])
    agent._get_provider_from_model = lambda name: "anthropic" if "claude" in name else "openai"
    agent._get_fallback_llm = AsyncMock(return_value=new)
    agent._emit_fallback_success_telemetry = Mock()
    agent._reconcile_native_tools = Mock()
    assert asyncio.run(ErrorRecoveryMixin._attempt_llm_fallback_in_handler(agent, "rate_limit"))
    assert agent.llm is new and agent.message_manager.llm is new
    assert agent.llm_provider == "anthropic"
    agent.message_manager.set_runtime_identity.assert_called_once_with(new.model_name, "anthropic")


def test_native_tool_mode_restores_requested_preference():
    from agents.task.agent.core.llm_provisioning import LLMProvisioningMixin
    agent = SimpleNamespace(
        use_native_tools=True, message_manager=SimpleNamespace(use_native_tools=True),
        controller=SimpleNamespace(supports_native_tools=lambda provider: provider == "native"),
        logger=logging.getLogger("native-mode"))
    assert not LLMProvisioningMixin._reconcile_native_tools(agent, "legacy")
    assert not agent.message_manager.use_native_tools
    assert LLMProvisioningMixin._reconcile_native_tools(agent, "native")
    assert agent.message_manager.use_native_tools


@pytest.mark.parametrize("correction", ["Found 3 researchers", "Did not find 5 researchers", "Found 5 engineers"])
def test_memory_preserves_distinct_facts_even_with_identical_embeddings(correction):
    from modules.memory.task.hierarchical_memory import HierarchicalMemory
    memory = HierarchicalMemory(session_id="fixture", task="fixture")
    memory.start_or_resume_phase("research", start_step=1)
    assert memory.add_finding_to_phase("research", "Found 5 researchers", embedding=[1.0, 0.0])
    assert memory.add_finding_to_phase("research", correction, embedding=[1.0, 0.0])
    assert not memory.add_finding_to_phase("research", correction, embedding=[1.0, 0.0])
