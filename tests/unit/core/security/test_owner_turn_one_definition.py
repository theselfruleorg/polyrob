"""W5 (D3): ONE "owner turn" definition — ``core.security.owner_turn``.

Every gate that asks "is the owner driving this turn?" reads the predicates in
that module; the old copies delegate. The cases below are the edges where the
copies used to disagree.
"""
from types import SimpleNamespace

import pytest

from core.security import owner_turn
from core.security.forged_turns import DELEGATION_RESULT_KIND, SELF_WAKE_KIND


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    import core.autonomy_marker as marker
    monkeypatch.setattr(marker, "is_autonomous", lambda sid: sid == "auto-sid")


def _ctx(**kw):
    base = dict(user_id="owner-1", role="orchestrator", is_sub_agent=False,
                session_id="chat-sid", metadata={})
    base.update(kw)
    return SimpleNamespace(**base)


def _refusal(ctx):
    return owner_turn.owner_turn_refusal(ctx, verb="v", does="does", public="public")


def test_legacy_owner_role_is_not_a_leaf():
    """The disagreement: role="owner" (the seats' old shape) was a genuine turn
    to the money gates and "a leaf agent" to owner_turn_refusal."""
    ctx = _ctx(role="owner")
    assert _refusal(ctx) is None
    assert owner_turn.is_genuine_owner_turn(ctx)
    assert not owner_turn.is_forged_or_autonomous(ctx)


@pytest.mark.parametrize("ctx, forged", [
    (_ctx(), False),
    (_ctx(is_sub_agent=True), True),
    (_ctx(role="leaf"), True),
    (SimpleNamespace(user_id="owner-1"), True),          # unset role = leaf
    (_ctx(metadata={"turn_kind": SELF_WAKE_KIND}), True),
    (_ctx(metadata={"turn_kind": DELEGATION_RESULT_KIND}), True),
    (_ctx(metadata={"turn_kind": "group"}), True),
    (_ctx(session_id="auto-sid"), True),
])
def test_forged_or_autonomous(ctx, forged):
    assert owner_turn.is_forged_or_autonomous(ctx) is forged


def test_autonomous_goal_turn_is_the_main_run_only():
    assert owner_turn.is_autonomous_goal_turn(_ctx(session_id="auto-sid"))
    assert not owner_turn.is_autonomous_goal_turn(_ctx())
    assert not owner_turn.is_autonomous_goal_turn(_ctx(session_id="auto-sid", role="leaf"))
    assert not owner_turn.is_autonomous_goal_turn(
        _ctx(session_id="auto-sid", metadata={"turn_kind": SELF_WAKE_KIND}))


def test_probe_fault_fails_closed(monkeypatch):
    import core.autonomy_marker as marker

    def boom(sid):
        raise RuntimeError("x")
    monkeypatch.setattr(marker, "is_autonomous", boom)
    assert owner_turn.is_forged_or_autonomous(_ctx()) is True
    assert owner_turn.is_autonomous_goal_turn(_ctx()) is False
    assert owner_turn.is_genuine_owner_turn(_ctx()) is False


def test_owner_turn_refusal_admits_an_autonomous_owner_run():
    assert _refusal(_ctx(session_id="auto-sid")) is None
    assert _refusal(_ctx(user_id="stranger")) == "v denied: only the owner tenant may public"
    assert _refusal(None) == "v requires an execution context"


@pytest.mark.parametrize("ctx", [
    _ctx(), _ctx(role="owner"), _ctx(role="leaf"), _ctx(is_sub_agent=True),
    _ctx(user_id="stranger"), _ctx(metadata={"turn_kind": SELF_WAKE_KIND}),
    _ctx(session_id="auto-sid"), None,
])
def test_every_copy_agrees(ctx, monkeypatch):
    """The delegators answer what the one definition answers."""
    import importlib
    cp = importlib.import_module("core.config_policy.compute_posture")
    from tools.controller.turn_origin import (
        _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)
    from tools.goal_tools import owner_seat_turn
    monkeypatch.setattr(cp, "compute_posture", lambda: 3)
    if ctx is not None:
        assert _is_forged_or_autonomous_turn(ctx, None) is \
            owner_turn.is_forged_or_autonomous(ctx)
        assert _is_autonomous_goal_turn(ctx, None) is owner_turn.is_autonomous_goal_turn(ctx)
    assert owner_seat_turn(ctx) is owner_turn.is_genuine_owner_turn(ctx)
    assert cp.compute_posture_allows(ctx, 1) is (_refusal(ctx) is None)


def test_x_browser_leaf_or_forged_is_the_shared_rule():
    from polyrob_x.x_browser.tool import _leaf_or_forged
    for ctx in (None, _ctx(), _ctx(role="leaf"), _ctx(metadata={"turn_kind": SELF_WAKE_KIND})):
        assert _leaf_or_forged(ctx) is owner_turn.is_leaf_or_forged(ctx)


def test_no_second_owner_turn_definition_in_the_x_pack():
    """The X pack reads the turn and its taint through polyrob.sdk, never the
    raw ``metadata['untrusted_read']`` key or the forged-kind literal."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[4] / "packs" / "x" / "polyrob_x"
    for path in (root / "twitter_tool.py", root / "x_browser" / "tool.py"):
        text = path.read_text()
        assert '"untrusted_read"' not in text, path
        assert "FORGED_TURN_KINDS" not in text, path
