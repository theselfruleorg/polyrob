"""End-to-end anchor for the 2026-09-13 dropped-image bug.

An owner photo reached prod as ``text=''`` and the empty-content guard dropped it
with no dispatch and no reply. These tests pin the whole telegram leg: the update
routes, the bytes land in the session workspace, and the turn carries both an
honest description and the vision block.
"""
import base64
import os

import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.media import Media
from surfaces.telegram.harness import act_on_inbound
from surfaces.telegram.inbound import InboundResult

_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _result(kind, *, text="", media=None, session_id=None):
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(
        text=text,
        identity=Identity(user_id="u_abc", source=src, raw_user_id="555"),
        media=media or [],
    )
    return InboundResult(
        inbound=inbound,
        decision=RouteDecision(kind, "agent:main:telegram:dm:555:u_abc",
                               session_id=session_id))


class _FakeAgent:
    def __init__(self):
        self.delivered = []
        self.created = []
    async def create_session(self, user_id, request=None, **kw):
        self.created.append({"user_id": user_id, "request": request})
        return {"id": "sess_new"}
    async def run_session(self, user_id, session_id):
        return "done"
    async def ensure_session_and_deliver(self, user_id, session_id, text, *,
                                         kind="comment", metadata=None):
        self.delivered.append({"session_id": session_id, "text": text,
                               "metadata": metadata})
        return "delivered"
    def touch_chat_binding(self, key):
        pass


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Point the path manager's workspace at a temp dir for every session id."""
    from agents.task import path as path_mod

    root = tmp_path / "ws"

    class _PM:
        def get_workspace_dir(self, session_id, user_id):
            d = root / str(user_id) / str(session_id)
            d.mkdir(parents=True, exist_ok=True)
            return d
        def clean_session_id(self, sid):
            return sid
    monkeypatch.setattr(path_mod, "pm", lambda: _PM())
    return root


async def _fetch(media):
    return _PNG if media.ref else None


@pytest.mark.asyncio
async def test_uncaptioned_photo_reaches_the_bound_session(workspace):
    agent = _FakeAgent()
    result = _result(RouteKind.STEER, text="", session_id="sess_live",
                     media=[Media(kind="image", ref="f1", filename="shot.png")])

    await act_on_inbound(agent, result, spawn=lambda c: c.close(),
                         fetch_media=_fetch)

    assert agent.delivered, "the photo turn was dropped — the 2026-09-13 bug"
    turn = agent.delivered[0]
    assert "shot.png" in turn["text"]
    assert turn["metadata"] and len(turn["metadata"]["image_attachments"]) == 1
    saved = workspace / "u_abc" / "sess_live" / "inbound" / "shot.png"
    assert saved.exists() and saved.read_bytes() == _PNG


@pytest.mark.asyncio
async def test_caption_and_image_ride_the_same_turn(workspace):
    agent = _FakeAgent()
    result = _result(RouteKind.STEER, text="what is this?", session_id="sess_live",
                     media=[Media(kind="image", ref="f1", filename="shot.png")])

    await act_on_inbound(agent, result, spawn=lambda c: c.close(), fetch_media=_fetch)

    turn = agent.delivered[0]
    assert turn["text"].startswith("what is this?")
    assert "shot.png" in turn["text"]


@pytest.mark.asyncio
async def test_undownloadable_attachment_is_named_never_silent(workspace):
    async def _no_bytes(media):
        return None
    agent = _FakeAgent()
    result = _result(RouteKind.STEER, text="see this", session_id="sess_live",
                     media=[Media(kind="document", ref="f1", filename="report.pdf")])

    await act_on_inbound(agent, result, spawn=lambda c: c.close(), fetch_media=_no_bytes)

    turn = agent.delivered[0]
    assert "report.pdf" in turn["text"]
    assert turn["metadata"] is None


@pytest.mark.asyncio
async def test_a_new_session_gets_the_attachment_queued_before_it_runs(workspace):
    agent = _FakeAgent()
    result = _result(RouteKind.TASK_AGENT, text="look",
                     media=[Media(kind="image", ref="f1", filename="shot.png")])

    await act_on_inbound(agent, result, spawn=lambda c: c.close(), fetch_media=_fetch)

    assert agent.created and agent.created[0]["request"] == "look"
    assert agent.delivered, "attachment was never queued into the fresh session"
    turn = agent.delivered[0]
    assert turn["session_id"] == "sess_new"
    assert "shot.png" in turn["text"]
    # The caption is the session's request; it must not be repeated on the turn.
    assert not turn["text"].startswith("look")
    assert turn["metadata"]["image_attachments"]


@pytest.mark.asyncio
async def test_a_text_only_turn_is_unchanged(workspace):
    agent = _FakeAgent()
    result = _result(RouteKind.STEER, text="just words", session_id="sess_live")

    await act_on_inbound(agent, result, spawn=lambda c: c.close(), fetch_media=_fetch)

    turn = agent.delivered[0]
    assert turn["text"] == "just words"
    assert turn["metadata"] is None


@pytest.mark.asyncio
async def test_no_fetcher_leaves_the_turn_untouched(workspace):
    agent = _FakeAgent()
    result = _result(RouteKind.STEER, text="hi", session_id="sess_live",
                     media=[Media(kind="image", ref="f1", filename="shot.png")])

    await act_on_inbound(agent, result, spawn=lambda c: c.close(), fetch_media=None)

    assert agent.delivered[0]["text"] == "hi"
