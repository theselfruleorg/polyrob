"""067 P1: the per-action policy registry (core/verb_policy.py).

The registry mechanics: all-or-nothing registration that refuses a duplicate or
a mis-owned row, the predicate query, the reserved set. The parity of the rows
against the emitted action names lives in test_action_name_parity.py; the
decision-level parity is the golden (tests/test_policy_decisions_golden.py).
"""
import pytest

from core import verb_policy as vp
from core.verb_policy import VerbPolicy


@pytest.fixture
def scratch_registry(monkeypatch):
    """An empty registry for the mechanics tests; the real one is restored."""
    monkeypatch.setattr(vp, "_REGISTRY", {})
    monkeypatch.setattr(vp, "_SOURCES", {})
    monkeypatch.setattr(vp, "_BUILT_VIEWS", {})
    return vp


def test_the_core_rows_loaded():
    assert len(vp.VERB_POLICY) > 200
    assert vp.policy_for("defi_trade_swap").tool == "defi_trade"
    assert vp.policy_for("message").tool is None
    assert vp.policy_for("no_such_action") is None


def test_registration_refuses_a_duplicate_name(scratch_registry):
    scratch_registry.register_verb_policy("t", [VerbPolicy("t_a", tool="t")], source="one")
    with pytest.raises(ValueError, match="already has a policy row from one"):
        scratch_registry.register_verb_policy(
            "t", [VerbPolicy("t_a", tool="t", effect="none")], source="two")


def test_registration_is_all_or_nothing(scratch_registry):
    with pytest.raises(ValueError, match="listed twice"):
        scratch_registry.register_verb_policy(
            "t", [VerbPolicy("t_b", tool="t"), VerbPolicy("t_b", tool="t")], source="x")
    with pytest.raises(ValueError, match="names tool"):
        scratch_registry.register_verb_policy(
            "t", [VerbPolicy("t_c", tool="t"), VerbPolicy("u_d", tool="u")], source="x")
    assert dict(scratch_registry._REGISTRY) == {}


def test_a_row_validates_its_values():
    with pytest.raises(ValueError):
        VerbPolicy("x", effect="teleport")
    with pytest.raises(ValueError):
        VerbPolicy(" x")


def test_ids_where_filters_by_value_and_callable(scratch_registry):
    scratch_registry.register_verb_policy("t", [
        VerbPolicy("t_read", tool="t", effect="none"),
        VerbPolicy("t_post", tool="t", effect="social"),
    ], source="x")
    assert scratch_registry.ids_where(effect="none") == {"t_read"}
    assert scratch_registry.ordered_ids_where(tool="t") == ("t_read", "t_post")
    assert scratch_registry.ids_where(effect=lambda e: e not in ("none", "inherit")) == {"t_post"}
    with pytest.raises(ValueError, match="unknown VerbPolicy field"):
        scratch_registry.ids_where(colour="red")


def test_reserved_names_have_rows_and_no_tool():
    assert vp.RESERVED_ACTION_NAMES
    for name in vp.RESERVED_ACTION_NAMES:
        row = vp.policy_for(name)
        assert row is not None and row.tool is None, name


def test_the_effect_views_are_the_rows():
    from core.effects import ACTION_EFFECTS, NON_WRITE_ACTIONS, WRITE_ACTIONS
    for row in vp.VERB_POLICY.values():
        in_views = (row.name in ACTION_EFFECTS
                    or any(row.name in s for s in NON_WRITE_ACTIONS.values())
                    or any(row.name in m for m in WRITE_ACTIONS.values()))
        assert in_views == (row.effect != "inherit"), row
    assert vp.EFFECT_CLASSES is __import__("core.effects", fromlist=["x"]).EFFECT_CLASSES


def test_a_row_validates_its_lane_fields():
    with pytest.raises(ValueError, match="unknown lane"):
        VerbPolicy("x", lane="fast")
    with pytest.raises(ValueError, match="verb_gate"):
        VerbPolicy("x", side="spend", approval_owner="verb")
    with pytest.raises(ValueError, match="verb_gate"):
        VerbPolicy("x", side="spend", approval_owner="hook", verb_gate="m::f")
    with pytest.raises(ValueError, match="needs a side"):
        VerbPolicy("x", approval_owner="hook")
    with pytest.raises(ValueError, match="defi lane"):
        VerbPolicy("x", lane="x402", simulatable=True)


def test_the_spend_and_payment_views_are_the_rows():
    from core.config_policy import payment_tools as pt
    from core.config_policy import spend_lane as sl

    rows = vp.VERB_POLICY.values()
    assert sl.DEFI_SPEND_VERBS == {r.name for r in rows if r.lane == "defi"}
    assert sl.ALWAYS_OWNER_APPROVED_VERBS == {r.name for r in rows if r.lane == "owner_always"}
    assert sl.X402_SPEND_VERBS == {r.name for r in rows if r.lane == "x402"}
    assert sl.DRY_RUN_VERBS == {r.name for r in rows if r.simulatable}
    assert sl._RISK_REDUCING_VERBS == {r.name for r in rows if r.risk_reducing}
    assert set(pt.PAYMENT_APPROVAL_TOOLS) == {r.name for r in rows if r.approval_owner == "hook"}
    assert set(pt.PAYMENT_RECEIVE_APPROVAL_TOOLS) == {
        r.name for r in rows if r.approval_owner == "hook" and r.side == "receive"}
    assert pt.VERB_OWNED_APPROVAL_GATES == {
        "defi_trade_bridge": "tools/defi/bridge_verb.py::_require_owner_approval"}


def test_the_approval_views_are_the_rows():
    from tools.controller import approval as ap

    rows = vp.VERB_POLICY.values()
    assert set(ap.DEFAULT_APPROVAL_REQUIRED_TOOLS) == {
        r.name for r in rows if "recommended" in r.approval}
    assert set(ap.POSTURE2_APPROVAL_REQUIRED_TOOLS) == {
        r.name for r in rows if "posture2" in r.approval}
    assert ap._ALWAYS_GATED_VERBS == {r.name for r in rows if "always_queued" in r.approval}
    with pytest.raises(ValueError, match="unknown approval lane"):
        VerbPolicy("x", approval=("sometimes",))


def test_the_room_view_is_the_rows():
    from core.surfaces.room_policy import ROOM_DENIED_ACTIONS

    assert ROOM_DENIED_ACTIONS == {r.name for r in vp.VERB_POLICY.values() if r.room_denied}


def test_the_correspondent_view_is_the_rows():
    from agents.task.agent.core.correspondent_gate import HIGH_IMPACT_TOOLS, _HIGH_IMPACT_NAMES

    assert _HIGH_IMPACT_NAMES == {
        r.name for r in vp.VERB_POLICY.values() if r.correspondent_blocked}
    assert HIGH_IMPACT_TOOLS is _HIGH_IMPACT_NAMES


def test_a_row_for_an_already_built_view_is_refused(scratch_registry):
    """067 P2: views are import-time snapshots; a late row that one of them would
    include is refused with the view's module, never silently missing."""
    scratch_registry.register_verb_policy("t", [VerbPolicy("t_a", tool="t")], source="x")
    assert scratch_registry.ids_where(room_denied=True) == frozenset()
    with pytest.raises(ValueError, match="would change a policy view .*test_verb_policy"):
        scratch_registry.register_verb_policy(
            "t", [VerbPolicy("t_b", tool="t", room_denied=True)], source="late")
    # A row no built view selects still registers.
    scratch_registry.register_verb_policy("t", [VerbPolicy("t_c", tool="t")], source="late")
    assert scratch_registry.policy_for("t_c") is not None
