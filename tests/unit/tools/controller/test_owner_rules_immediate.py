"""Model-authored persistent rules require a separate owner review."""
import logging
import types

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from tools.controller.registry.service import Registry
from tools.controller.service import Controller


def test_the_dead_apply_now_path_is_gone():
    """095 W10 (X-9): a model-written rule is always a draft; the "apply now"
    branches had no live caller and are deleted, not kept behind a False."""
    import tools.controller.doc_authoring as da
    from core.self_context_writer import SelfContextWriter
    for name in ("_rules_immediate", "_applied_now_text", "perform_rules_propose"):
        assert not hasattr(da, name), name
    assert not hasattr(SelfContextWriter, "apply_now")


def _bare_controller(data_dir):
    c = object.__new__(Controller)
    c.logger = logging.getLogger("rules-immediate-test")
    c.registry = Registry()
    c.user_id = "tenant-A"
    c.session_id = "s1"
    c.container = types.SimpleNamespace(
        config=types.SimpleNamespace(data_dir=str(data_dir)))
    return c


def _owner_ctx(uid="alice"):
    return types.SimpleNamespace(user_id=uid, is_sub_agent=False,
                                 role="orchestrator", metadata={})


def _forged_ctx(uid="alice"):
    return types.SimpleNamespace(user_id=uid, is_sub_agent=False,
                                 role="orchestrator",
                                 metadata={"turn_kind": "self_wake"})


def _paths(tmp_path, name, uid="alice"):
    root = tmp_path / "identity" / "polyrob" / f"user_{uid}"
    return root / name, root / ".pending" / name


def _action(c, which):
    getattr(c, f"_register_{which}_action")()
    return c.registry.registry.actions[which]


@pytest.mark.asyncio
@pytest.mark.parametrize("which,name,enable", [
    ("owner_doc_manage", "owner.md", "OWNER_DOC_WRITABLE"),
    ("self_context_manage", "self.md", "SELF_CONTEXT_WRITABLE"),
])
async def test_model_rule_requires_review_even_on_owner_turn(monkeypatch, tmp_path, which, name, enable):
    monkeypatch.setenv(enable, "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    c = _bare_controller(tmp_path)
    action = _action(c, which)
    result = await action.function(
        action.param_model(reason="test reason", action="update", content="Never publish customer records."),
        execution_context=_owner_ctx())
    active, pending = _paths(tmp_path, name)
    assert result.error is None
    assert pending.exists() and not active.exists()
    assert "NOT YET IN EFFECT" in result.extracted_content
    promoted = await action.function(action.param_model(action="promote"), execution_context=_owner_ctx())
    assert promoted.error and not active.exists()
    # The direct owner review primitive still activates the reviewed draft.
    from core import self_evolution
    kind = "owner_doc" if which == "owner_doc_manage" else "self_context"
    [item] = self_evolution.list_pending("alice", home_dir=tmp_path, instance_id="polyrob")
    ok, message = self_evolution.promote(kind, "alice", user_id="alice", home_dir=tmp_path,
                                         instance_id="polyrob", rev=item["rev"])
    assert ok, message
    assert active.exists()


@pytest.mark.asyncio
async def test_new_draft_never_replaces_active_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("OWNER_DOC_WRITABLE", "true")
    from core.owner_doc_writer import OwnerDocWriter
    writer = OwnerDocWriter(tmp_path, instance_id="polyrob")
    writer.propose("Keep customer data private.", user_id="alice", created_by="agent", pending=False)
    action = _action(_bare_controller(tmp_path), "owner_doc_manage")
    for context in (_owner_ctx(), _forged_ctx()):
        result = await action.function(action.param_model(reason="test reason", action="update", content="Publish all records."),
                                       execution_context=context)
        assert result.error is None
        active, pending = _paths(tmp_path, "owner.md")
        assert "Keep customer data private." in active.read_text()
        assert pending.exists()
