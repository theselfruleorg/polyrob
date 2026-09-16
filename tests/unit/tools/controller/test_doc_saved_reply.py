"""035 P0-4 — a quarantined write must not report as done.

On 2026-09-08/09 the agent told the owner "Self-context saved (pending review;
applies next session)" for two "stop posting to the den" directives. The owner
read that as done; both sat inert in `.pending/` for two days while the agent
kept posting. The reply has to say it is NOT in effect and name the command.
"""
import logging
import types

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from tools.controller.registry.service import Registry
from tools.controller.service import Controller


def _bare_controller(data_dir):
    c = object.__new__(Controller)
    c.logger = logging.getLogger("doc-reply-test")
    c.registry = Registry()
    c.user_id = "tenant-A"
    c.session_id = "s1"
    c.container = types.SimpleNamespace(
        config=types.SimpleNamespace(data_dir=str(data_dir)))
    return c


def _ctx(uid="rob"):
    return types.SimpleNamespace(user_id=uid, is_sub_agent=False,
                                 role="orchestrator", metadata={})


@pytest.mark.asyncio
async def test_self_context_reply_states_inertness_and_command(monkeypatch, tmp_path):
    monkeypatch.setenv("SELF_CONTEXT_WRITABLE", "true")
    monkeypatch.setenv("SELF_CONTEXT_REQUIRE_REVIEW", "true")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    c = _bare_controller(tmp_path)
    c._register_self_context_manage_action()
    action = c.registry.registry.actions["self_context_manage"]
    res = await action.function(
        action.param_model(action="update", content="STOP posting to the den."),
        execution_context=_ctx())
    body = res.extracted_content or ""
    assert "NOT YET IN EFFECT" in body
    assert "/approve self_context:rob" in body
    assert "saved (pending review" not in body


@pytest.mark.asyncio
async def test_owner_doc_reply_states_inertness_and_command(monkeypatch, tmp_path):
    monkeypatch.setenv("OWNER_DOC_WRITABLE", "true")
    monkeypatch.setenv("OWNER_DOC_REQUIRE_REVIEW", "true")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    c = _bare_controller(tmp_path)
    c._register_owner_doc_manage_action()
    action = c.registry.registry.actions["owner_doc_manage"]
    res = await action.function(
        action.param_model(action="update", content="Owner prefers concise answers."),
        execution_context=_ctx())
    body = res.extracted_content or ""
    assert "NOT YET IN EFFECT" in body
    assert "/approve owner_doc:rob" in body
