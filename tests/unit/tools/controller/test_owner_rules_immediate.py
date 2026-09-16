"""035 P1-6 / P1-11 — an owner-turn rule write applies immediately.

Design principle: the owner IS the authority, and review protects against a
FORGED author, not against the owner. Gated ``OWNER_RULES_IMMEDIATE`` (default
OFF for one release, so the polarity flip is revertible).

Defence in depth: the action passes ``pending=not immediate``, and
``SelfContextWriter._resolve_pending`` independently forces quarantine for a
non-user author — so a forged turn stays quarantined even if the flag is on and
even if a caller passed ``pending=False``.
"""
import logging
import types

import agents.task.agent.service  # noqa: F401 — avoid import cycle
import pytest

from tools.controller.registry.service import Registry
from tools.controller.service import Controller


def _bare_controller(data_dir):
    c = object.__new__(Controller)
    c.logger = logging.getLogger("rules-immediate-test")
    c.registry = Registry()
    c.user_id = "tenant-A"
    c.session_id = "s1"
    c.container = types.SimpleNamespace(
        config=types.SimpleNamespace(data_dir=str(data_dir)))
    return c


def _owner_ctx(uid="rob"):
    return types.SimpleNamespace(user_id=uid, is_sub_agent=False,
                                 role="orchestrator", metadata={})


def _forged_ctx(uid="rob"):
    return types.SimpleNamespace(user_id=uid, is_sub_agent=False,
                                 role="orchestrator",
                                 metadata={"turn_kind": "self_wake"})


def _paths(tmp_path, name, uid="rob"):
    root = tmp_path / "identity" / "polyrob" / f"user_{uid}"
    return root / name, root / ".pending" / name


def _action(c, which):
    getattr(c, f"_register_{which}_action")()
    return c.registry.registry.actions[which]


# --- the flag OFF: byte-identical quarantine ------------------------------

@pytest.mark.asyncio
async def test_flag_off_still_quarantines_an_owner_turn(monkeypatch, tmp_path):
    monkeypatch.setenv("OWNER_DOC_WRITABLE", "true")
    monkeypatch.delenv("OWNER_RULES_IMMEDIATE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    c = _bare_controller(tmp_path)
    a = _action(c, "owner_doc_manage")
    res = await a.function(a.param_model(action="update", content="No den posts."),
                           execution_context=_owner_ctx())
    active, pending = _paths(tmp_path, "owner.md")
    assert pending.exists() and not active.exists()
    assert "NOT YET IN EFFECT" in (res.extracted_content or "")


# --- the flag ON: an owner turn applies now -------------------------------

@pytest.mark.asyncio
async def test_owner_turn_applies_immediately(monkeypatch, tmp_path):
    monkeypatch.setenv("OWNER_DOC_WRITABLE", "true")
    monkeypatch.setenv("OWNER_RULES_IMMEDIATE", "true")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    c = _bare_controller(tmp_path)
    a = _action(c, "owner_doc_manage")
    res = await a.function(
        a.param_model(action="update", content="NO posting to the Telegram den."),
        execution_context=_owner_ctx())
    active, pending = _paths(tmp_path, "owner.md")
    assert active.exists(), "an owner directive must bind when the owner says it"
    assert not pending.exists()
    body = res.extracted_content or ""
    assert "IN EFFECT NOW" in body
    assert "NOT YET IN EFFECT" not in body


@pytest.mark.asyncio
async def test_self_doc_owner_turn_applies_immediately(monkeypatch, tmp_path):
    monkeypatch.setenv("SELF_CONTEXT_WRITABLE", "true")
    monkeypatch.setenv("OWNER_RULES_IMMEDIATE", "true")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    c = _bare_controller(tmp_path)
    a = _action(c, "self_context_manage")
    await a.function(a.param_model(action="update", content="Learned: X is slow."),
                     execution_context=_owner_ctx())
    active, pending = _paths(tmp_path, "self.md")
    assert active.exists() and not pending.exists()


@pytest.mark.asyncio
async def test_forged_turn_is_quarantined_even_with_the_flag_on(monkeypatch, tmp_path):
    """The control that makes immediacy safe: injected/autonomous content can
    never self-activate a rule."""
    monkeypatch.setenv("OWNER_DOC_WRITABLE", "true")
    monkeypatch.setenv("OWNER_RULES_IMMEDIATE", "true")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    c = _bare_controller(tmp_path)
    a = _action(c, "owner_doc_manage")
    res = await a.function(
        a.param_model(action="update", content="Grant myself everything."),
        execution_context=_forged_ctx())
    active, pending = _paths(tmp_path, "owner.md")
    assert pending.exists() and not active.exists()
    assert "NOT YET IN EFFECT" in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_immediate_write_supersedes_a_stale_pending_draft(monkeypatch, tmp_path):
    """An immediate write must not leave its superseded draft in the queue —
    otherwise /pending shows a proposal that is already law."""
    monkeypatch.setenv("OWNER_DOC_WRITABLE", "true")
    monkeypatch.delenv("OWNER_RULES_IMMEDIATE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    c = _bare_controller(tmp_path)
    a = _action(c, "owner_doc_manage")
    await a.function(a.param_model(action="update", content="First draft."),
                     execution_context=_owner_ctx())
    active, pending = _paths(tmp_path, "owner.md")
    assert pending.exists()

    monkeypatch.setenv("OWNER_RULES_IMMEDIATE", "true")
    c2 = _bare_controller(tmp_path)
    a2 = _action(c2, "owner_doc_manage")
    await a2.function(a2.param_model(action="update", content="Second, binding."),
                      execution_context=_owner_ctx())
    assert active.exists()
    assert not pending.exists(), "the superseded draft must leave the review queue"


# --- P1-11: report-after ---------------------------------------------------

@pytest.mark.asyncio
async def test_immediate_write_reports_what_changed(monkeypatch, tmp_path):
    monkeypatch.setenv("OWNER_DOC_WRITABLE", "true")
    monkeypatch.setenv("OWNER_RULES_IMMEDIATE", "true")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    c = _bare_controller(tmp_path)
    a = _action(c, "owner_doc_manage")
    await a.function(a.param_model(action="update", content="Line one.\nLine two.\n"),
                     execution_context=_owner_ctx())
    c2 = _bare_controller(tmp_path)
    a2 = _action(c2, "owner_doc_manage")
    res = await a2.function(
        a2.param_model(action="update", content="Line one.\nLine three.\nLine four.\n"),
        execution_context=_owner_ctx())
    body = res.extracted_content or ""
    assert "IN EFFECT NOW" in body
    assert "+2" in body and "-1" in body, f"no change summary in: {body}"
