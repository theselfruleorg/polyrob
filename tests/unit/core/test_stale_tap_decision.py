"""A stale ``/approve_p_<hex>`` tap names the decision that already consumed it.

2026-10-15 06:42: the owner tapped an approval the maintenance loop had promoted
at 05:07 and got "No pending proposal … see /pending", which reads as lost.
"""
import asyncio
from types import SimpleNamespace

from core import self_evolution
from core.self_evolution import last_decision_for_alias, pending_tap_alias


def _ev(action, kind, item_id, ts, source="owner_review", pending=False):
    return {"ts": ts, "source": source,
            "attrs": {"kind": kind, "action": action, "item_id": item_id,
                      "pending": pending}}


def test_a_decided_alias_names_its_decision():
    alias = pending_tap_alias("skill", "promo-bounty")
    events = [_ev("promote", "skill", "promo-bounty", 1760505000.0)]
    hit = last_decision_for_alias(alias, user_id="rob", events=events)
    assert hit == {"action": "promote", "kind": "skill",
                   "item_id": "promo-bounty", "ts": 1760505000.0}


def test_the_underscore_tap_form_resolves_too():
    alias = pending_tap_alias("skill", "promo-bounty").replace("-", "_")
    events = [_ev("reject", "skill", "promo-bounty", 1.0)]
    assert last_decision_for_alias(alias, user_id="rob", events=events)["action"] == "reject"


def test_the_newest_decision_wins():
    alias = pending_tap_alias("owner_doc", "rob")
    events = [_ev("promote", "owner_doc", "rob", 2.0),
              _ev("reject", "owner_doc", "rob", 1.0)]
    assert last_decision_for_alias(alias, user_id="rob", events=events)["ts"] == 2.0


def test_an_agent_proposal_is_not_a_decision():
    alias = pending_tap_alias("owner_doc", "rob")
    events = [_ev("patch", "owner_doc", "rob", 3.0, source="owner_doc_manage",
                  pending=True)]
    assert last_decision_for_alias(alias, user_id="rob", events=events) is None


def test_an_unknown_or_non_alias_target_is_none():
    events = [_ev("promote", "skill", "x", 1.0)]
    assert last_decision_for_alias("p-000000", user_id="rob", events=events) is None
    assert last_decision_for_alias("skill:x", user_id="rob", events=events) is None


def test_decide_verb_answers_already_approved(monkeypatch):
    from surfaces.telegram import approval_ops
    alias = pending_tap_alias("skill", "promo-bounty")
    monkeypatch.setattr(approval_ops, "_pending_set",
                        lambda *a, **k: SimpleNamespace(items=[],
                                                        degraded_line=lambda: ""))
    monkeypatch.setattr(self_evolution, "last_decision_for_alias",
                        lambda t, user_id: {"action": "promote", "kind": "skill",
                                            "item_id": "promo-bounty",
                                            "ts": 1760504859.0})
    c = SimpleNamespace(cmd="/approve", args=[alias], user_id="rob", data_dir="/x",
                        instance_id="rob", board=None, task_agent=None)
    out = asyncio.run(approval_ops.decide_verb(c))
    assert out.startswith("Already approved at 2025-10-15 05:07Z — skill:promo-bounty")
