"""W5 (D3): TurnFacts, built once; one owner-seat context constructor."""
from types import SimpleNamespace

import pytest

from core.security import turn_facts
from core.security.turn_facts import facts_of, owner_seat_context


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("SECURITY_SETTINGS", raising=False)
    import core.autonomy_marker as marker
    monkeypatch.setattr(marker, "is_autonomous", lambda sid: sid == "auto-sid")
    from core.security import refusal_taint
    monkeypatch.setattr(refusal_taint, "is_tainted", lambda sid: False)


def test_seat_context_shape():
    ctx = owner_seat_context("owner-1", seat="telegram")
    assert ctx.role == "orchestrator" and ctx.is_sub_agent is False
    assert ctx.metadata["turn_kind"] == turn_facts.OWNER_SEAT_KIND
    assert ctx.metadata["owner_seat"] == "telegram"
    # never the host-shell seat key
    assert "seat" not in ctx.metadata


def test_each_seat_call_is_its_own_turn():
    a = owner_seat_context("owner-1", seat="cli")
    b = owner_seat_context("owner-1", seat="cli")
    assert a.metadata["turn_id"] != b.metadata["turn_id"]
    assert owner_seat_context("o", seat="cli", turn_id="t1").metadata["turn_id"] == "t1"


def test_unknown_seat_is_refused():
    with pytest.raises(ValueError):
        owner_seat_context("owner-1", seat="terminal")


@pytest.mark.parametrize("seat", turn_facts.SEATS)
def test_seat_context_is_the_owner_driving(seat):
    facts = facts_of(owner_seat_context("owner-1", seat=seat))
    assert facts.origin == "owner"
    assert facts.principal == "owner-1"
    assert facts.seat == seat
    assert facts.owner_driving


def test_origins():
    base = dict(user_id="owner-1", role="orchestrator", is_sub_agent=False,
                session_id="s", metadata={})
    assert facts_of(None).origin == "direct"
    assert facts_of(SimpleNamespace(**{**base, "role": "leaf"})).origin == "leaf"
    assert facts_of(SimpleNamespace(**{**base, "metadata": {"turn_kind": "self_wake"}})).origin \
        == "forged"
    assert facts_of(SimpleNamespace(**{**base, "metadata": {"turn_kind": "group"}})).origin \
        == "room"
    assert facts_of(SimpleNamespace(**{**base, "session_id": "auto-sid"})).origin == "autonomous"
    orch = SimpleNamespace(_correspondent_tainted=True, session_id="s")
    f = facts_of(SimpleNamespace(**base), orchestrator=orch)
    assert f.origin == "correspondent" and f.taints.correspondent and not f.owner_driving


def test_seat_contexts_have_one_shape():
    """Every seat helper returns the one constructor's shape."""
    from cli.commands import wallet
    from surfaces.telegram import send_ops, token_ops
    from core.wallet.token_trust import owner_seat_ctx
    for ctx in (token_ops._owner_ctx("owner-1"), send_ops._owner_ctx("owner-1"),
                wallet._owner_ctx(), owner_seat_ctx("owner-1")):
        assert ctx.role == "orchestrator"
        assert ctx.metadata["turn_kind"] == turn_facts.OWNER_SEAT_KIND
        assert ctx.metadata["turn_id"]
