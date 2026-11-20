"""127 W3 — the agent knows, in the same session, whether an owner-rule draft waits.

2026-11-21: the owner approved a draft at 11-20 18:23Z outside the agent's turn;
the agent's history still held "NOT YET IN EFFECT … tap", and it told the owner
"waiting on your tap". The state delta now heads the owner rules with one status
line, and a long owner.md sends what changed instead of a cut-off copy."""
from types import SimpleNamespace

from core.owner_doc_writer import OwnerDocWriter
from core.status_rules import owner_draft_status

TS = 1792171013.0  # 2026-10-16 17:16:53Z


def _decision(action="promote", kind="owner_doc"):
    return {"ts": TS, "source": "owner_review",
            "attrs": {"kind": kind, "action": action, "item_id": "u1", "pending": False}}


def test_a_waiting_draft_is_named_with_its_taps(tmp_path):
    w = OwnerDocWriter(str(tmp_path), instance_id="i")
    assert w.propose("- a rule\n", user_id="u1", created_by="agent", pending=True).pending
    line = owner_draft_status("u1", home_dir=str(tmp_path), instance_id="i", events=[])
    assert line.startswith("Owner-rule drafts: 1 pending since ")
    assert "NOT in effect" in line and "/approve_p_" in line and "/reject_p_" in line


def test_the_last_decision_is_named_when_nothing_waits(tmp_path):
    line = owner_draft_status("u1", home_dir=str(tmp_path), instance_id="i",
                              events=[_decision("reject", kind="skill"), _decision()])
    assert line == ("Owner-rule drafts: none pending; the owner approved the last one "
                    "10-16 17:16Z. Nothing waits for a tap.")
    line = owner_draft_status("u1", home_dir=str(tmp_path), instance_id="i",
                              events=[_decision("reject")])
    assert "the owner rejected the last one" in line


def test_nothing_ever_written_says_none(tmp_path):
    assert owner_draft_status("u1", home_dir=str(tmp_path), instance_id="i", events=[]) == \
        "Owner-rule drafts: none pending. Nothing waits for a tap."


def test_an_unreadable_store_is_not_an_empty_one(monkeypatch, tmp_path):
    from core import self_evolution as se

    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(se, "_owner_writer", boom)
    assert "unreadable" in owner_draft_status("u1", home_dir=str(tmp_path), instance_id="i")


# --- the state-delta part -------------------------------------------------------
def _part(monkeypatch, agent, owner):
    import core.status_rules as SR
    from agents.task.agent.core import state_delta as SD
    monkeypatch.setattr(SR, "owner_draft_status", lambda *a, **k: "Owner-rule drafts: none pending.")
    return SD._owner_rules_part(agent, owner, "u1", "/h", "i")


def test_a_short_doc_is_sent_whole_under_the_status(monkeypatch):
    part = _part(monkeypatch, SimpleNamespace(), "- rule A")
    assert part == "## Owner rules (current)\nOwner-rule drafts: none pending.\n\n- rule A"


def test_a_long_doc_sends_what_changed(monkeypatch):
    agent = SimpleNamespace()
    old = "\n".join(f"- standing rule number {i} about the work" for i in range(120))
    first = _part(monkeypatch, agent, old)
    assert "full rules are in the ## Owner rules block" in first and len(first) < 4000
    new = old.replace("- standing rule number 7 about the work", "- rule seven, replaced")
    part = _part(monkeypatch, agent, new)
    assert part.startswith("## Owner rules (changed)\nOwner-rule drafts: none pending.")
    assert "+ - rule seven, replaced" in part and "- - standing rule number 7 about" in part
    assert part.endswith("All rules: owner_doc_manage action='read'.") and len(part) < 4000
    # A preferences write wakes the check with the rules unchanged: the same text, so
    # the part is not pushed again as "(no rule line changed)".
    assert _part(monkeypatch, agent, new) == part


def test_the_state_carries_a_flag_not_only_words(tmp_path, monkeypatch):
    """127 W3: a caller reads the flag (owner_draft_state), never the line's wording."""
    import core.status_rules as SR
    pending, line = SR.owner_draft_state("u1", home_dir=str(tmp_path), instance_id="i", events=[])
    assert pending is False and "none pending" in line
