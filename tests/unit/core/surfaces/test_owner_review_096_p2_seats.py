"""096 C1/C2: "read it" on a pending contact or a signer approval opens the
whole record on the phone, not the one-line list summary."""
import types

import pytest

from surfaces.telegram import approval_ops


@pytest.fixture
def opened(monkeypatch, tmp_path):
    def _open(match, container=None):
        monkeypatch.setattr("tools.controller.approval_queue.resolve_pending_target",
                            lambda target, pending: match)
        c = types.SimpleNamespace(user_id="alice", data_dir=str(tmp_path),
                                  instance_id="agent1", container=container)
        return approval_ops._review_one(c, types.SimpleNamespace(items=[match]), "x")
    return _open, tmp_path


def test_signer_item_opens_the_signers_own_review(opened, monkeypatch):
    _open, _ = opened
    summary = "send 2 ETH to 0xabc " + "detail " * 60
    monkeypatch.setattr("core.signer.approvals.review",
                        lambda aid, client=None: (True, f"signer approval {aid} — evm.send on base\n{summary}"))
    out = _open({"kind": "signer_approval", "id": "s1", "preview": "short"})
    assert summary in out and "/approve_" in out and "/reject_" in out


def test_signer_item_names_an_unreadable_signer(opened, monkeypatch):
    _open, _ = opened
    monkeypatch.setattr("core.signer.approvals.review",
                        lambda aid, client=None: (False, "polyrob-signer did not answer"))
    out = _open({"kind": "signer_approval", "id": "s1", "preview": "signer review: $9.00"})
    assert "signer review: $9.00" in out and "did not answer" in out


def test_contact_item_opens_who_why_and_the_first_message(opened):
    _open, home = opened
    from core.surfaces.conversations import ConversationStore
    store = ConversationStore(str(home / "conversations.db"))
    store.record_outbound("alice", "email", "bob@x.io", "Hello Bob, the whole first message.",
                          subject="Intro", session_id="sess1")
    out = _open({"kind": "correspondent", "id": "email:bob@x.io", "session_id": "sess1",
                 "provenance": "owner", "preview": "email:bob@x.io -> session sess1"})
    assert "email bob@x.io" in out and "session sess1" in out
    assert "Hello Bob, the whole first message." in out and "Subject: Intro" in out
    assert "/approve_" in out and "/reject_" in out


def test_contact_item_without_a_store_says_not_recorded(opened):
    _open, _ = opened
    out = _open({"kind": "correspondent", "id": "email:bob@x.io", "session_id": "s",
                 "provenance": "owner"})
    assert "First message: (not recorded)" in out


def test_contact_first_message_is_the_oldest_one_kept(opened):
    _open, home = opened
    from core.surfaces.conversations import ConversationStore
    store = ConversationStore(str(home / "conversations.db"))
    store.record_outbound("alice", "email", "bob@x.io", "FIRST hello", session_id="s")
    for i in range(60):                              # more than one history page
        store.record_outbound("alice", "email", "bob@x.io", f"later {i}", session_id="s")
    out = _open({"kind": "correspondent", "id": "email:bob@x.io", "session_id": "s",
                 "provenance": "owner"})
    assert "FIRST hello" in out and "later 0" not in out


def test_long_first_message_cut_has_a_more_tap():
    from core.surfaces import more_store
    from core.surfaces.seed import FIRST_MESSAGE_INLINE_MAX_CHARS, build_contact_notice
    first = "w" * (FIRST_MESSAGE_INLINE_MAX_CHARS + 100)
    out = build_contact_notice({"kind": "correspondent", "id": "email:b@x.io"},
                               surface="email", address="b@x.io", session_id="s",
                               provenance="owner", first_message=first, more_tenant="alice")
    token = out.split("/more_", 1)[1].split()[0].rstrip(")")
    assert more_store.fetch("alice", token) == first
