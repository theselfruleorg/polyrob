"""W5 (D3): one owner-seat context constructor."""
import pytest

from core.security import turn_facts
from core.security.turn_facts import owner_seat_context


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("SECURITY_SETTINGS", raising=False)
    import core.autonomy_marker as marker
    monkeypatch.setattr(marker, "is_autonomous", lambda sid: sid == "auto-sid")


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
def test_seat_context_is_a_genuine_owner_turn(seat):
    from core.security.owner_turn import is_genuine_owner_turn
    assert is_genuine_owner_turn(owner_seat_context("owner-1", seat=seat))


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
