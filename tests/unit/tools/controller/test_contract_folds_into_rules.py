"""035 P1-7 — the CONTRACT lane folds into RULES.

`contract.md` had a real reader (`load_contract_doc`, CONTRACT_DOC_ENABLED
default ON) and, measured over the subsystem's entire life on prod, ZERO
writes — a dead lane behind live plumbing, costing a document kind and a
tool-description slot. `contract_propose` now writes the RULES doc (owner.md),
so no new contract draft is ever created; an EXISTING contract draft stays
listable and promotable for one release (035 §6 migration step 2).
"""
import logging
import types

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from tools.controller.registry.service import Registry
from tools.controller.service import Controller


def _c(data_dir):
    c = object.__new__(Controller)
    c.logger = logging.getLogger("contract-fold-test")
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
async def test_contract_propose_writes_the_rules_doc(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFS_TOOL_ENABLED", "true")
    monkeypatch.delenv("OWNER_RULES_IMMEDIATE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    c = _c(tmp_path)
    c._register_preferences_action()
    a = c.registry.registry.actions["preferences"]
    res = await a.function(
        a.param_model(operation="contract_propose",
                      text="Never post to the Telegram public den."),
        execution_context=_ctx())
    root = tmp_path / "identity" / "polyrob" / "user_rob"
    assert (root / ".pending" / "owner.md").is_file(), \
        "an operating rule now lands in the RULES doc"
    assert not (root / ".pending" / "contract.md").exists(), \
        "no NEW contract draft may be created"
    assert res.error is None


@pytest.mark.asyncio
async def test_contract_propose_applies_now_on_an_owner_turn(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFS_TOOL_ENABLED", "true")
    monkeypatch.setenv("OWNER_RULES_IMMEDIATE", "true")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    c = _c(tmp_path)
    c._register_preferences_action()
    a = c.registry.registry.actions["preferences"]
    await a.function(
        a.param_model(operation="contract_propose", text="Never post to the den."),
        execution_context=_ctx())
    root = tmp_path / "identity" / "polyrob" / "user_rob"
    assert (root / "owner.md").is_file(), "an owner turn binds its own rule"


def test_an_existing_contract_draft_stays_promotable(tmp_path):
    """Migration guarantee: this release must not orphan a queued proposal."""
    from core.contract_writer import ContractWriter
    from core.instance import DEFAULT_INSTANCE_ID
    from core import self_evolution as se
    ContractWriter(tmp_path, instance_id=DEFAULT_INSTANCE_ID).propose(
        "legacy operating rules", user_id="rob", created_by="agent", pending=True)
    items = se.list_pending("rob", home_dir=tmp_path,
                            instance_id=DEFAULT_INSTANCE_ID, skill_manager=None)
    assert any(i["kind"] == "contract" for i in items)
    ok, _ = se.promote("contract", "rob", user_id="rob", home_dir=tmp_path,
                       instance_id=DEFAULT_INSTANCE_ID)
    assert ok
