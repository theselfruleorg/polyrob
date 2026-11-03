"""035 P1-7 — the CONTRACT lane folds into RULES.

`contract.md` had a real reader (`load_contract_doc`, CONTRACT_DOC_ENABLED
default ON) and, measured over the subsystem's entire life on prod, ZERO
writes — a dead lane behind live plumbing, costing a document kind and a
tool-description slot. `contract_propose` wrote the RULES doc (owner.md) from
035 until 095 W10 deleted it; no new contract draft is ever created, and an
EXISTING contract draft stays listable and promotable.
"""
import logging
import types

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from tools.controller.registry.service import Registry
from tools.controller.service import Controller


@pytest.fixture(autouse=True)
def _identity_home(tmp_path, monkeypatch):
    """095 W8: the preferences action reads the ONE identity home (``prefs_home_dir``)."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))


def _c(data_dir):
    c = object.__new__(Controller)
    c.logger = logging.getLogger("contract-fold-test")
    c.registry = Registry()
    c.user_id = "tenant-A"
    c.session_id = "s1"
    c.container = types.SimpleNamespace(
        config=types.SimpleNamespace(data_dir=str(data_dir)))
    return c


def _ctx(uid="alice"):
    return types.SimpleNamespace(user_id=uid, is_sub_agent=False,
                                 role="orchestrator", metadata={})


def test_contract_propose_is_gone(monkeypatch, tmp_path):
    """095 W10 (X-21): the second owner.md writer skipped the rules sweep and
    needed the FULL body. `owner_doc_manage` is the one rule writer."""
    monkeypatch.setenv("PREFS_TOOL_ENABLED", "true")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    c = _c(tmp_path)
    c._register_preferences_action()
    a = c.registry.registry.actions["preferences"]
    with pytest.raises(Exception):
        a.param_model(operation="contract_propose", text="Never post to the den.")
    import tools.controller.doc_authoring as da
    assert not hasattr(da, "perform_rules_propose")


def test_an_existing_contract_draft_stays_promotable(tmp_path):
    """Migration guarantee: this release must not orphan a queued proposal."""
    from core.contract_writer import ContractWriter
    from core.instance import DEFAULT_INSTANCE_ID
    from core import self_evolution as se
    ContractWriter(tmp_path, instance_id=DEFAULT_INSTANCE_ID).propose(
        "legacy operating rules", user_id="alice", created_by="agent", pending=True)
    items = se.list_pending("alice", home_dir=tmp_path,
                            instance_id=DEFAULT_INSTANCE_ID, skill_manager=None)
    assert any(i["kind"] == "contract" for i in items)
    [rev] = [i["rev"] for i in items if i["kind"] == "contract"]
    ok, _ = se.promote("contract", "alice", user_id="alice", home_dir=tmp_path,
                       instance_id=DEFAULT_INSTANCE_ID, rev=rev)
    assert ok
