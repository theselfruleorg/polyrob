"""044 T14 behavioural: a member's @mention in an allowlisted room, end to end
through ``handle_update``.

Proves the three things a unit test of ``render_context`` cannot:
  (a) ``deliver_group_turn`` is reached with a context block that carries an
      EARLIER member's line and the attributed ``<addressed>`` line;
  (b) the run is spawned as the session's OWNER, never as the speaking member
      (a room key is not user-scoped — the tenant is the session's, not the
      sender's);
  (c) a member's line never becomes a steer.

Reuses the FakeBot/FakeDedup/FakeUD rig from test_harness_progress.py.
"""
import time
import types

import pytest

from core.surfaces.dispatcher import RouteKind
from core.surfaces.group_ledger import GroupLedger, LedgerRow
from surfaces.telegram.harness import TelegramHarness
from tests.unit.surfaces.telegram.test_harness_progress import (
    FakeBot,
    FakeDedup,
    FakeUD,
    _drain,
)

_ROOM_CHAT_ID = "-100123"
_ROOM_SESSION_KEY = "agent:main:telegram:group:-100123"
_OWNER_UID = "u_owner"
_MEMBER_UID = "u_member"


def _allow_this_room(tmp_path):
    """044 I2: an unlisted room is dropped BEFORE `process_update` now (no paid
    voice download, no user-directory row for a stranger), so a rig that stubs
    `process_update` must allowlist the room it is testing — which is what
    production looks like anyway: a room turn only ever happens in an allowed
    room."""
    import os
    from core.surfaces.group_allowlist import GroupAllowlist
    GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db")).allow(
        "telegram", _ROOM_CHAT_ID)



class _Container:
    def __init__(self, ledger):
        self._svc = {"group_ledger": ledger}

    def get_service(self, n):
        return self._svc.get(n)


class _FakeTaskAgent:
    def __init__(self):
        self.container = None
        self.group_turns = []
        self.runs = []
        self.steers = []
        self.pushed_context = []
        self.created = []
        self.session_manager = types.SimpleNamespace(
            get_session_info=lambda sid: {"id": sid, "user_id": _OWNER_UID})

    async def deliver_group_turn(self, session_id, *, user_id, context_block,
                                 addressed_block, role, reply_to=None, metadata=None,
                                 surface=None, chat_id=None, shown_message_ids=None):
        self.group_turns.append(dict(session_id=session_id, user_id=user_id,
                                     context_block=context_block,
                                     addressed_block=addressed_block, role=role,
                                     reply_to=reply_to, surface=surface,
                                     chat_id=chat_id,
                                     shown_message_ids=tuple(shown_message_ids or ())))
        return "delivered"

    def push_room_context(self, session_id, context_block, *, orchestrator=None):
        self.pushed_context.append((session_id, context_block))
        return bool(context_block)

    async def create_session(self, user_id, *, request=None, session_source=None,
                             chat_session_key=None, tool_ids=None):
        self.created.append(dict(user_id=user_id, request=request,
                                 tool_ids=tuple(tool_ids or ())))
        return {"id": "sess-cold-1"}

    def set_turn_reply_to(self, session_id, mid):
        pass

    async def ensure_session_and_deliver(self, user_id, session_id, text, *,
                                         kind="comment", metadata=None):
        self.steers.append((user_id, text))
        return "delivered"

    def touch_chat_binding(self, key):
        pass

    async def run_session(self, user_id, session_id):
        self.runs.append((user_id, session_id))
        return "Session completed successfully"

    def get_orchestrator(self, session_id):
        return None


def _member_update(uid=1, text="@bot which chains?"):
    return {"update_id": uid, "message": {
        "message_id": 77,
        "chat": {"id": int(_ROOM_CHAT_ID), "type": "supergroup",
                 "title": "The Public Den"},
        "from": {"id": 9911, "username": "bob"}, "text": text,
        "date": time.time()}}


def _result(update):
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
    src = SessionSource(surface_id="telegram", chat_id=_ROOM_CHAT_ID,
                        chat_type="supergroup")
    ident = Identity(user_id=_MEMBER_UID, source=src, raw_user_id="9911",
                     display_name="bob")
    inbound = InboundMessage(text=update["message"]["text"], identity=ident,
                             raw=update, mentions_bot=True)
    decision = types.SimpleNamespace(kind=RouteKind.GROUP_TURN,
                                     session_key=_ROOM_SESSION_KEY,
                                     session_id="sess-room-1", command=None,
                                     silent=False)
    return types.SimpleNamespace(inbound=inbound, decision=decision)


def _patch(monkeypatch, *, result):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "9911,777")
    import surfaces.telegram.inbound as inbound_mod
    import surfaces.telegram.surface as surface_mod

    async def fake_process_update(*a, **k):
        return result
    monkeypatch.setattr(inbound_mod, "process_update", fake_process_update)
    monkeypatch.setattr(surface_mod, "chat_id_from_session_key",
                        lambda key: _ROOM_CHAT_ID)


@pytest.mark.asyncio
async def test_member_mention_runs_a_room_turn_with_the_earlier_line(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", _OWNER_UID)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    _allow_this_room(tmp_path)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)

    ledger = GroupLedger(str(tmp_path / "surfaces.db"))
    ledger.append(LedgerRow("telegram", _ROOM_CHAT_ID, None, "76", time.time(),
                            "8123", "@alice", False, "member", "text",
                            "anyone tried the bridge?", None, False))

    update = _member_update()
    result = _result(update)
    _patch(monkeypatch, result=result)

    bot = FakeBot()
    harness = TelegramHarness(bot, _Container(ledger), object(), webhook_base=None,
                              dedup=FakeDedup(), user_directory=FakeUD())
    ta = _FakeTaskAgent()
    ta.container = harness.container
    harness.task_agent = ta
    await harness.handle_update(update)
    await _drain()

    assert len(ta.group_turns) == 1, "a member's mention did not run a room turn"
    turn = ta.group_turns[0]
    assert turn["session_id"] == "sess-room-1"
    assert turn["role"] == "member", "a member's line must never be a steer"
    # (a) the context block carries the EARLIER member's line, attributed.
    assert '<group-context chat="The Public Den" surface="telegram">' in turn["context_block"]
    assert "@alice|8123 (member): anyone tried the bridge?" in turn["context_block"]
    # ... and never the addressed message itself (it is rendered once, below).
    assert "which chains?" not in turn["context_block"]
    assert turn["addressed_block"] == (
        "<addressed>\n@bob|9911 (member) → you: @bot which chains?\n</addressed>")
    assert turn["reply_to"] == "77", "a room reply must thread to the line it answers"
    # (b) the run is the SESSION's owner, not the member who spoke.
    assert ta.runs == [(_OWNER_UID, "sess-room-1")]
    assert ta.steers == []


@pytest.mark.asyncio
async def test_a_room_turn_shows_the_working_bubble(monkeypatch, tmp_path):
    """044 T3: GROUP_TURN is a routed TURN, so it gets the visible side effects
    (the '⚙️ Working…' bubble) a DENIED room line never gets."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", _OWNER_UID)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    _allow_this_room(tmp_path)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)

    ledger = GroupLedger(str(tmp_path / "surfaces.db"))
    update = _member_update()
    _patch(monkeypatch, result=_result(update))

    bot = FakeBot()
    harness = TelegramHarness(bot, _Container(ledger), object(), webhook_base=None,
                              dedup=FakeDedup(), user_directory=FakeUD())
    ta = _FakeTaskAgent()
    ta.container = harness.container
    harness.task_agent = ta
    await harness.handle_update(update)
    await _drain()

    assert any(c[0] == "send" and "Working" in str(c[2]) for c in bot.calls), bot.calls


@pytest.mark.asyncio
async def test_an_empty_ledger_still_answers(monkeypatch, tmp_path):
    """Fail-open: no room log (or an unreadable one) costs the CONTEXT, never
    the turn."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", _OWNER_UID)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    _allow_this_room(tmp_path)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)

    update = _member_update()
    _patch(monkeypatch, result=_result(update))

    bot = FakeBot()
    harness = TelegramHarness(bot, _Container(None), object(), webhook_base=None,
                              dedup=FakeDedup(), user_directory=FakeUD())
    ta = _FakeTaskAgent()
    ta.container = harness.container
    harness.task_agent = ta
    await harness.handle_update(update)
    await _drain()

    assert len(ta.group_turns) == 1
    assert ta.group_turns[0]["context_block"] == ""
    assert "which chains?" in ta.group_turns[0]["addressed_block"]


def _cold_result(update):
    """A member's line in a room with NO bound session — routed TASK_AGENT since
    044 T14 (a member may START the room session)."""
    r = _result(update)
    r.decision.kind = RouteKind.TASK_AGENT
    r.decision.session_id = None
    return r


@pytest.mark.asyncio
async def test_a_cold_room_start_never_stores_the_context_block(monkeypatch, tmp_path):
    """Fix round 1, IMPORTANT 5: the context block rode `create_session(request=…)`,
    so a room's recent chatter became the session TASK — durable for the life of
    the session, which is exactly what "the context block is API-only" forbids."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", _OWNER_UID)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    _allow_this_room(tmp_path)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)

    ledger = GroupLedger(str(tmp_path / "surfaces.db"))
    ledger.append(LedgerRow("telegram", _ROOM_CHAT_ID, None, "76", time.time(),
                            "8123", "@alice", False, "member", "text",
                            "anyone tried the bridge?", None, False))

    update = _member_update()
    _patch(monkeypatch, result=_cold_result(update))

    bot = FakeBot()
    harness = TelegramHarness(bot, _Container(ledger), object(), webhook_base=None,
                              dedup=FakeDedup(), user_directory=FakeUD())
    ta = _FakeTaskAgent()
    ta.container = harness.container
    harness.task_agent = ta
    await harness.handle_update(update)
    await _drain()

    assert len(ta.created) == 1, ta.created
    request = ta.created[0]["request"]
    assert "<group-context" not in request, request
    assert "anyone tried the bridge?" not in request
    assert "<addressed>" in request and "which chains?" in request
    # ... and the block WAS shown — as an ephemeral, after the session exists.
    assert len(ta.pushed_context) == 1
    sid, ctx = ta.pushed_context[0]
    assert sid == "sess-cold-1"
    assert "@alice|8123 (member): anyone tried the bridge?" in ctx
    # The cold start runs as the OWNER tenant, not the member who opened it.
    assert ta.created[0]["user_id"] == _OWNER_UID
    assert ta.runs == [(_OWNER_UID, "sess-cold-1")]


@pytest.mark.asyncio
async def test_a_cold_room_start_marks_the_lines_it_showed(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", _OWNER_UID)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    _allow_this_room(tmp_path)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)

    ledger = GroupLedger(str(tmp_path / "surfaces.db"))
    ledger.append(LedgerRow("telegram", _ROOM_CHAT_ID, None, "76", time.time(),
                            "8123", "@alice", False, "member", "text",
                            "anyone tried the bridge?", None, False))

    update = _member_update()
    _patch(monkeypatch, result=_cold_result(update))

    bot = FakeBot()
    harness = TelegramHarness(bot, _Container(ledger), object(), webhook_base=None,
                              dedup=FakeDedup(), user_directory=FakeUD())
    ta = _FakeTaskAgent()
    ta.container = harness.container
    harness.task_agent = ta
    await harness.handle_update(update)
    await _drain()

    # The shown line is now answered, so the NEXT turn's unanswered tail is empty.
    assert ledger.tail("telegram", _ROOM_CHAT_ID, unanswered_only=True) == []
    assert ledger.tail("telegram", _ROOM_CHAT_ID)[0].answered_by == "sess-cold-1"


# ---------------------------------------------------------------------------
# 044 I8 — "presented = handled" needs the block to have been PRESENTED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_dropped_context_block_does_not_retire_the_lines(monkeypatch, tmp_path):
    """`push_room_context` is fail-open and returns False when it could not place
    the block. The marking ran anyway, so a DROPPED block permanently retired
    ledger lines the model never saw — gone from the next turn and from the
    service run alike. The ADDRESSED line is still marked (it rode the session's
    own task text, so it WAS delivered)."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", _OWNER_UID)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    _allow_this_room(tmp_path)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)

    ledger = GroupLedger(str(tmp_path / "surfaces.db"))
    ledger.append(LedgerRow("telegram", _ROOM_CHAT_ID, None, "76", time.time(),
                            "8123", "@alice", False, "member", "text",
                            "anyone tried the bridge?", None, False))

    update = _member_update()
    _patch(monkeypatch, result=_cold_result(update))

    harness = TelegramHarness(FakeBot(), _Container(ledger), object(),
                              webhook_base=None, dedup=FakeDedup(),
                              user_directory=FakeUD())
    ta = _FakeTaskAgent()
    ta.container = harness.container
    # The failure mode: the block could not be placed.
    ta.push_room_context = lambda *a, **k: False
    harness.task_agent = ta
    await harness.handle_update(update)
    await _drain()

    rows = {r.message_id: r for r in ledger.tail("telegram", _ROOM_CHAT_ID)}
    assert rows["76"].answered_by in (None, ""), (
        "a line the model was never shown was marked answered")
    assert rows["77"].answered_by == "sess-cold-1", (
        "the addressed line WAS delivered and must not be re-answered later")
