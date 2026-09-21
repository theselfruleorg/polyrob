"""D53, D57, D79 and the `/cwd` read — the Telegram harness's honest states.

Three silent drops and one duplicated decider:

* **D53** — a CORRESPONDENT's attachment was not absorbed (correct: a third
  party's bytes never enter the owner tenant's workspace) and was not NAMED
  either, so a captionless photo arrived as an empty turn and the agent could
  not answer "you did not send it" when something had been sent.
* **D57** — an anonymous-admin line was dropped BEFORE the ledger, making
  `docs/guide/groups.md`'s "every allowed-room line reaches the room log before
  any gate" false for exactly the line an admin is most likely to post.
* **D79** — `/approve all` ran its own loop beside
  `approval_queue.decide_all_pending`. Two implementations of "decide
  everything" is how "all" came to mean the self-evolution THIRD of the queue
  on one seat and the whole union on another.
"""
import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.media import Media
from surfaces.telegram.inbound import InboundResult


def _result(*, text="", media=None, kind=RouteKind.CORRESPONDENT_DATA,
            session_id="sess-1"):
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(
        text=text,
        identity=Identity(user_id="u_them", source=src, raw_user_id="555"),
        media=media or [])
    return InboundResult(
        inbound=inbound,
        decision=RouteDecision(kind, "agent:main:telegram:dm:555:u_them",
                               session_id=session_id))


# --- D53 --------------------------------------------------------------------

def test_a_correspondent_attachment_is_named_on_the_turn():
    from surfaces.telegram.harness import _correspondent_text
    out = _correspondent_text(_result(
        text="have a look",
        media=[Media(kind="document", ref="f1", filename="offer.pdf",
                     mime="application/pdf")]))
    assert out.startswith("have a look")
    assert "offer.pdf" in out
    assert "did NOT download" in out


def test_a_captionless_correspondent_photo_is_never_an_empty_turn():
    """The shape that produced silence: a photo carries no `text` at all."""
    from surfaces.telegram.harness import _correspondent_text
    out = _correspondent_text(_result(
        media=[Media(kind="image", ref="f1", filename="shot.png")]))
    assert out.strip()
    assert "shot.png" in out


def test_a_text_only_correspondent_reply_is_unchanged():
    from surfaces.telegram.harness import _correspondent_text
    assert _correspondent_text(_result(text="just words")) == "just words"


@pytest.mark.asyncio
async def test_the_correspondent_rail_delivers_the_manifest():
    """End-to-end: the manifest reaches `deliver_correspondent_data`, which is
    the only thing the originating session ever sees."""
    from surfaces.telegram.harness import act_on_inbound
    seen = {}

    class _Agent:
        async def deliver_correspondent_data(self, session_id, src, text, **kw):
            seen["text"] = text

    await act_on_inbound(_Agent(), _result(
        media=[Media(kind="image", ref="f1", filename="shot.png")]))
    assert "shot.png" in seen["text"]


# --- D57 --------------------------------------------------------------------

def test_an_anonymous_room_line_is_ledgered(tmp_path, monkeypatch):
    import os
    from core.surfaces.group_allowlist import GroupAllowlist
    from core.surfaces.group_ledger import GroupLedger
    from core.surfaces.ledger_ingest import record_anonymous_to_ledger

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db")).allow(
        "telegram", "-100", "The Room")
    ledger = GroupLedger(os.path.join(str(tmp_path), "surfaces.db"))

    class _Container:
        config = None

        def get_service(self, name):
            return ledger if name == "group_ledger" else None

    import time
    update = {"message": {"message_id": 9, "date": time.time(),
                          "chat": {"id": -100, "type": "supergroup",
                                   "title": "The Room"},
                          "sender_chat": {"id": -100, "title": "The Room"},
                          "text": "everyone please read the pinned post"}}
    assert record_anonymous_to_ledger(_Container(), surface="telegram",
                                      raw_update=update) is True
    rows = ledger.tail("telegram", "-100", limit=5)
    assert len(rows) == 1
    assert rows[0].text == "everyone please read the pinned post"
    # An unauthenticated line is never more than a member's, and names nobody.
    assert rows[0].role_at_write == "member"
    assert rows[0].sender_id == ""
    assert rows[0].sender_name == "The Room"


def test_an_anonymous_line_from_an_unallowed_room_is_never_stored(tmp_path, monkeypatch):
    import os
    from core.surfaces.group_ledger import GroupLedger
    from core.surfaces.ledger_ingest import record_anonymous_to_ledger

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    ledger = GroupLedger(os.path.join(str(tmp_path), "surfaces.db"))

    class _Container:
        config = None

        def get_service(self, name):
            return ledger if name == "group_ledger" else None

    import time
    update = {"message": {"message_id": 9, "date": time.time(),
                          "chat": {"id": -100, "type": "supergroup"},
                          "text": "hello"}}
    assert record_anonymous_to_ledger(_Container(), surface="telegram",
                                      raw_update=update) is False
    assert ledger.tail("telegram", "-100", limit=5) == []


def test_an_empty_anonymous_line_is_never_a_blank_row(tmp_path, monkeypatch):
    import os
    from core.surfaces.group_allowlist import GroupAllowlist
    from core.surfaces.group_ledger import GroupLedger
    from core.surfaces.ledger_ingest import record_anonymous_to_ledger

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db")).allow(
        "telegram", "-100", "The Room")
    ledger = GroupLedger(os.path.join(str(tmp_path), "surfaces.db"))

    class _Container:
        config = None

        def get_service(self, name):
            return ledger if name == "group_ledger" else None

    import time
    update = {"message": {"message_id": 9, "date": time.time(),
                          "chat": {"id": -100, "type": "supergroup"}}}
    assert record_anonymous_to_ledger(_Container(), surface="telegram",
                                      raw_update=update) is False


# --- D79 --------------------------------------------------------------------

def test_approve_all_uses_the_shared_decider():
    """The contract, read from the source: a second implementation of "decide
    everything" is the bug, not the loop's details."""
    import inspect
    from surfaces.telegram import harness
    src = inspect.getsource(harness._handle_owner_admin)
    assert "decide_all_pending" in src
    # the private loop is gone
    assert "for it in list(pending.items):" not in src


# --- /cwd (C67) --------------------------------------------------------------

def test_cwd_names_all_three_answers():
    """Three different things hide behind "where are you working". Picking one
    and passing it off as the answer is how an owner debugs the wrong tree."""
    from surfaces.telegram.harness import _cwd_reply
    out = _cwd_reply()
    assert "process directory" in out
    assert "data home" in out
    assert "instance" in out
