"""098 CO-19: an owner decision on an identity draft binds to the revision the
owner reviewed. A changed draft is refused (and re-sent); a draft write that
races a promote is serialised by the per-doc lock and never lost."""
from __future__ import annotations

import threading

import pytest

from core import self_evolution as se
from core.contract_writer import ContractWriter
from core.owner_doc_writer import OwnerDocWriter
from core.self_context_writer import (
    DRAFT_CHANGED, PROVENANCE_BACKGROUND, SelfContextWriter, draft_rev,
)

WRITERS = [SelfContextWriter, OwnerDocWriter, ContractWriter]


@pytest.fixture(autouse=True)
def _clean_scan(monkeypatch):
    import modules.memory.task.threat_scan as ts
    monkeypatch.setattr(ts, "is_identity_suspicious", lambda body: False)


def _draft(w, uid="u1", text="- prefers short replies\n"):
    res = w.propose(text, user_id=uid, created_by=PROVENANCE_BACKGROUND)
    assert res.ok and res.pending
    return w.list_pending(uid)["rev"]


def test_promote_refuses_changed_draft(tmp_path):
    w = SelfContextWriter(tmp_path, "i1")
    rev = _draft(w)
    w.patch(user_id="u1", old_string="short", new_string="long",
            created_by=PROVENANCE_BACKGROUND)
    res = w.promote(user_id="u1", expected_rev=rev)
    assert not res.ok and res.errors == [DRAFT_CHANGED]
    assert w.read_active("u1") == ""
    assert w.list_pending("u1") is not None  # still pending, for a fresh review


def test_promote_with_matching_rev_activates_that_text(tmp_path):
    w = SelfContextWriter(tmp_path, "i1")
    rev = _draft(w)
    res = w.promote(user_id="u1", expected_rev=rev)
    assert res.ok and not res.pending
    assert "prefers short replies" in w.read_active("u1")
    assert w.list_pending("u1") is None


def test_reject_refuses_changed_draft(tmp_path):
    w = SelfContextWriter(tmp_path, "i1")
    rev = _draft(w)
    w.patch(user_id="u1", old_string="short", new_string="long",
            created_by=PROVENANCE_BACKGROUND)
    assert w.reject(user_id="u1", expected_rev=rev).errors == [DRAFT_CHANGED]
    assert w.reject(user_id="u1", expected_rev=w.list_pending("u1")["rev"]).ok


def test_replaced_draft_is_archived(tmp_path):
    w = SelfContextWriter(tmp_path, "i1")
    _draft(w)
    w.patch(user_id="u1", old_string="short", new_string="long",
            created_by=PROVENANCE_BACKGROUND)
    arch = list((w._root("u1") / ".archived").glob("rejected.*.md"))
    assert len(arch) == 1 and "short" in arch[0].read_text()


def test_patch_during_promote_is_archived_not_lost(tmp_path, monkeypatch):
    """A patch that arrives between promote's read and its remove waits on the
    per-doc lock; it then lands as a NEW draft, never deleted."""
    w = SelfContextWriter(tmp_path, "i1")
    rev = _draft(w)
    orig = w._rebase
    box = {}

    def racing_rebase(uid, draft):
        def _patch():
            box["res"] = SelfContextWriter(tmp_path, "i1").propose(
                "- prefers short replies\n- injected rule\n", user_id="u1",
                created_by=PROVENANCE_BACKGROUND)
        t = threading.Thread(target=_patch)
        t.start()
        t.join(0.3)
        box["blocked"] = t.is_alive()
        box["t"] = t
        return orig(uid, draft)

    monkeypatch.setattr(w, "_rebase", racing_rebase)
    res = w.promote(user_id="u1", expected_rev=rev)
    box["t"].join(5)
    assert res.ok
    assert box["blocked"] is True
    assert "injected rule" not in w.read_active("u1")
    # The racing write is the next draft, not lost.
    assert "injected rule" in w._pending_file("u1").read_text()
    assert box["res"].ok and box["res"].pending


@pytest.mark.parametrize("cls", WRITERS)
def test_owner_doc_and_contract_writer_inherit_rev_check(tmp_path, cls):
    w = cls(tmp_path, "i1")
    rev = _draft(w, text="- rule one\n")
    assert w.list_pending("u1")["rev"] == rev == draft_rev(w._pending_file("u1").read_text())
    assert w.promote(user_id="u1", expected_rev="0" * 16).errors == [DRAFT_CHANGED]
    assert w.promote(user_id="u1", expected_rev=rev).ok


def test_owner_seat_rev_in_item_id(tmp_path):
    w = SelfContextWriter(tmp_path, "i1")
    rev = _draft(w)
    items = se.list_pending("u1", home_dir=tmp_path, instance_id="i1")
    assert items[0]["rev"] == rev
    ok, msg = se.promote("self_context", "u1@" + "0" * 16, user_id="u1",
                         home_dir=tmp_path, instance_id="i1")
    assert not ok and "changed" in msg
    ok, _ = se.promote("self_context", "u1", rev=rev, user_id="u1",
                       home_dir=tmp_path, instance_id="i1")
    assert ok


def test_notified_card_binds_a_seat_without_rev(tmp_path):
    """A tap from the inbox card (no rev) decides the revision the owner was
    notified of; a draft changed after the notice is refused and re-sent."""
    w = SelfContextWriter(tmp_path, "i1")
    _draft(w)
    items = se.list_pending("u1", home_dir=tmp_path, instance_id="i1")
    se.save_notified_state("u1", se.pending_notify_state(items),
                           home_dir=tmp_path, instance_id="i1")
    w.patch(user_id="u1", old_string="short", new_string="long",
            created_by=PROVENANCE_BACKGROUND)
    ok, msg = se.promote("self_context", "u1", user_id="u1", home_dir=tmp_path,
                         instance_id="i1")
    assert not ok and "changed" in msg
    # The notified record is cleared, so the notifier re-sends the new draft.
    fresh = se.list_pending("u1", home_dir=tmp_path, instance_id="i1")
    assert se.pending_needs_notification(
        fresh, se.load_notified_state("u1", home_dir=tmp_path, instance_id="i1"))
    assert w.read_active("u1") == ""
