"""Round-2 review: the email owner-resend cooldown must compare the SAME string
the conversation store records.

`EmailTool.send_email` records `params.body` (`store.record_outbound(..., params.body)`)
but the first pass at the content-aware cooldown passed `f"{subject}\\n{body}"`.
The hashes could never match, so the gate — which exists because a
completion-judge retry delivered the owner the same report twice within minutes
on 2026-08-28 — silently stopped firing for email entirely.

A content gate that reads a different string than the writer wrote is not a
loosened gate; it is a dead one that still looks present.
"""
import pytest

from tools.controller.action_registration import _autonomous_owner_resend_cooldown_refusal


class _Ctx:
    is_sub_agent = True
    role = "leaf"
    metadata: dict = {}


class _Container:
    def __init__(self, store):
        self._store = store

    def get_service(self, name):
        return self._store if name == "conversation_store" else None


_OWNER = {"email": "owner@example.com"}


@pytest.fixture()
def store(tmp_path):
    from core.surfaces.conversations import ConversationStore
    return ConversationStore(str(tmp_path / "c.db"))


def _refusal(store, text):
    return _autonomous_owner_resend_cooldown_refusal(
        _Ctx(), None, container=_Container(store), user_id="rob",
        surface="email", target="owner@example.com", owner_targets=_OWNER,
        text=text, event_log=None)


def test_a_repeat_email_body_is_refused(store, monkeypatch):
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    body = "Track-record post BLOCKED — both X rails down."
    # exactly what EmailTool.send_email records after a successful send
    store.record_outbound("rob", "email", "owner@example.com", body)
    assert _refusal(store, body) is not None


def test_a_new_email_body_proceeds(store, monkeypatch):
    monkeypatch.setenv("OWNER_MESSAGE_COOLDOWN_SEC", "7200")
    store.record_outbound("rob", "email", "owner@example.com", "the daily summary")
    assert _refusal(store, "X posting is BLOCKED: both rails dead") is None


def test_the_email_tool_passes_the_body_it_records():
    """Pinned at the source: the two strings are written by different lines and
    nothing else makes them agree."""
    import inspect
    import tools.email_tool as et
    src = inspect.getsource(et)
    assert "text=params.body" in src, \
        "the cooldown must be given the same string record_outbound stores"
    assert 'store.record_outbound(user_id, "email", params.to, params.body' in src
