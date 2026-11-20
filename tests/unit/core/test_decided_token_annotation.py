"""An owner-bound text never asks for a tap the owner already gave.

2026-10-17 01:52Z: the agent sent "Still waiting on your one tap …: /approve_p_a9d770";
the owner had approved it at 10-16 17:16:53Z (owner_review promote event).
"""
from core.self_evolution import annotate_decided_tokens, pending_tap_alias

TS = 1792171013.0  # 2026-10-16 17:16:53Z


def _promote_event(kind="skill", item="promo-bounty", action="promote"):
    return {"ts": TS, "source": "owner_review",
            "attrs": {"kind": kind, "action": action, "item_id": item, "pending": False}}


def _token(kind="skill", item="promo-bounty"):
    return "/approve_" + pending_tap_alias(kind, item).replace("-", "_")


def test_decided_token_is_rewritten():
    tok = _token()
    out = annotate_decided_tokens(f"Still waiting on your one tap: {tok}. Thanks",
                                  user_id="alice", live_items=[], events=[_promote_event()])
    assert tok not in out
    assert "already approved 10-16 17:16Z" in out
    assert ". Thanks" in out
    # 127 W3: the sentence that asked for the tap is gone, so nothing contradicts it.
    assert "waiting" not in out and "Correction" not in out


def test_the_2026_11_21_parenthetical_is_replaced_whole():
    """'owner.md — your standing rule (waiting on your tap: /approve_p_x)' became
    '(waiting on your tap: (already approved …))' plus a Correction line."""
    tok = _token(kind="owner_doc", item="alice")
    text = f"3. owner.md — your standing rule (waiting on your tap: {tok}).\n4. reports/x.md"
    out = annotate_decided_tokens(text, user_id="alice", live_items=[],
                                  events=[_promote_event(kind="owner_doc", item="alice")])
    assert out == ("3. owner.md — your standing rule (already approved 10-16 17:16Z — in effect)."
                   "\n4. reports/x.md")


def test_pending_wording_elsewhere_still_gets_the_correction():
    tok = _token()
    text = f"The rule is still pending.\nTap {tok}"
    out = annotate_decided_tokens(text, user_id="alice", live_items=[], events=[_promote_event()])
    assert tok not in out and "That item is already approved" in out
    assert "Correction: that item was already approved 10-16 17:16Z" in out


def test_a_list_item_keeps_its_marker():
    tok = _token()
    out = annotate_decided_tokens(f"- tap {tok} to activate\n- next", user_id="alice",
                                  live_items=[], events=[_promote_event()])
    assert out.startswith("- That item is already approved") and out.endswith("\n- next")


def test_live_token_is_untouched_even_with_an_old_decision():
    tok = _token()
    live = [{"kind": "skill", "id": "promo-bounty"}]  # re-proposed: same alias
    text = f"tap {tok}"
    assert annotate_decided_tokens(text, user_id="alice", live_items=live,
                                   events=[_promote_event()]) == text


def test_unknown_token_is_untouched():
    text = "tap /approve_p_abcdef"
    assert annotate_decided_tokens(text, user_id="alice", live_items=[], events=[]) == text


def test_rejected_item_reads_rejected():
    tok = _token().replace("/approve_", "/reject_")
    out = annotate_decided_tokens(f"or {tok}", user_id="alice", live_items=[],
                                  events=[_promote_event(action="reject")])
    assert "already rejected" in out
