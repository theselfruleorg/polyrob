"""P4: harness dispatch — derive_webhook_path + act_on_inbound (decision -> task_agent).

These are the deterministic, no-network parts of the Telegram webhook harness. The
aiogram Bot/Dispatcher/set_webhook round-trip itself needs a live bot token to verify;
this locks the route derivation (no hardcoded /mvpbot) and the decision->action mapping
(STEER steers the bound session, TASK_AGENT creates+runs with the binding kwargs,
COMMAND dispatches, warm-but-dead rehydrates instead of diverting).
"""
import pytest

from surfaces.telegram.harness import (
    derive_webhook_path, act_on_inbound, owner_allowed,
    TelegramHarness, build_telegram_harness,
)
from surfaces.telegram.inbound import InboundResult
from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import InboundMessage, Identity, SessionSource


def _result(kind, *, text="hello", session_id=None, command=None,
            user="u_abc", chat="555", key="agent:main:telegram:dm:555:u_abc"):
    src = SessionSource("telegram", chat, "dm")
    inbound = InboundMessage(text=text, identity=Identity(user_id=user, source=src, raw_user_id="555"))
    return InboundResult(inbound=inbound, decision=RouteDecision(kind, key, session_id=session_id, command=command))


class _FakeOrch:
    def __init__(self): self.submitted = []
    async def submit_user_message(self, agent_id, text, **kw): self.submitted.append((agent_id, text, kw))


class _FakeTaskAgent:
    def __init__(self, orch=None, deliver_ok=True):
        self._orch = orch
        self.created = []
        self.ran = []
        self.cancelled = []
        self.delivered = []
        self.deliver_ok = deliver_ok  # whether the bound session is resident/recreatable
    def get_orchestrator(self, session_id): return self._orch
    async def create_session(self, user_id, request=None, **kwargs):
        self.created.append({"user_id": user_id, "request": request, "kwargs": kwargs})
        return {"id": "sess_new"}
    async def run_session(self, user_id, session_id):
        self.ran.append((user_id, session_id))
        # Real run_session returns a generic STATUS string (not the agent's answer);
        # the reply-delivery fix must NOT send this — it must extract the real reply.
        return "Session completed successfully"
    async def cancel_session_by_id(self, session_id, force=False): self.cancelled.append(session_id); return True
    async def ensure_session_and_deliver(self, user_id, session_id, text, *, kind="comment", metadata=None):
        self.delivered.append({"user_id": user_id, "session_id": session_id, "text": text, "kind": kind})
        # a-MED2: real method returns a status string; keep the bool shim for the
        # existing deliver_ok tests (True -> delivered, False -> gone).
        if isinstance(self.deliver_ok, str):
            return self.deliver_ok
        return "delivered" if self.deliver_ok else "gone"
    def touch_chat_binding(self, session_key):
        self.touched = getattr(self, "touched", [])
        self.touched.append(session_key)
    def unbind_chat(self, session_key):
        self.unbound = getattr(self, "unbound", [])
        self.unbound.append(session_key)
    def _extract_chat_reply(self, session_id):
        # Mirror TaskAgent._extract_chat_reply: the agent's REAL last reply, NOT
        # run_session's generic status string. Tests set ``ta.reply``.
        return getattr(self, "reply", "")


# --- derive_webhook_path -----------------------------------------------------

def test_webhook_path_from_env(monkeypatch):
    monkeypatch.setenv("WEBHOOK_PATH", "/tg/hook")
    assert derive_webhook_path() == "/tg/hook"


def test_webhook_path_default_is_not_mvpbot(monkeypatch):
    monkeypatch.delenv("WEBHOOK_PATH", raising=False)
    p = derive_webhook_path()
    assert p and p != "/mvpbot"        # the old bot's hardcoded-route bug must not recur
    assert p.startswith("/")


def test_webhook_path_normalizes_leading_slash(monkeypatch):
    monkeypatch.setenv("WEBHOOK_PATH", "tg/hook")
    assert derive_webhook_path() == "/tg/hook"


# --- act_on_inbound ----------------------------------------------------------

@pytest.mark.asyncio
async def test_steer_delivers_to_bound_session_and_resumes():
    """STEER delivers into the BOUND session (resident OR recreated-from-disk) and
    re-runs it by the ORIGINAL session_id — never mints a new session."""
    ta = _FakeTaskAgent(deliver_ok=True)
    spawned = []
    await act_on_inbound(ta, _result(RouteKind.STEER, text="keep going", session_id="sess_1"),
                         spawn=spawned.append)
    assert ta.delivered and ta.delivered[0]["session_id"] == "sess_1"
    assert ta.delivered[0]["text"] == "keep going"
    assert ta.created == []        # no NEW session minted
    assert len(spawned) == 1       # run_session re-run on the bound id
    for c in spawned: c.close()


@pytest.mark.asyncio
async def test_task_agent_creates_with_binding_kwargs_then_runs():
    ta = _FakeTaskAgent()
    spawned = []
    await act_on_inbound(ta, _result(RouteKind.TASK_AGENT, text="scrape x"), spawn=spawned.append)
    assert len(ta.created) == 1
    kw = ta.created[0]["kwargs"]
    assert kw["chat_session_key"] == "agent:main:telegram:dm:555:u_abc"
    assert kw["session_source"].surface_id == "telegram"
    assert ta.created[0]["request"] == "scrape x"
    assert len(spawned) == 1  # run_session spawned
    for c in spawned: c.close()


@pytest.mark.asyncio
async def test_owner_task_session_gets_goal_and_mission_toolset(monkeypatch):
    """THE FIX: the OWNER's interactive session is created with a toolset that includes
    `goal` (+ mission tools) so it can introspect its board via goal_list instead of
    reading the sandbox filesystem and hallucinating 'goal database is empty'."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    ta = _FakeTaskAgent()
    spawned = []
    await act_on_inbound(
        ta, _result(RouteKind.TASK_AGENT, text="review your goals", user="rob"),
        spawn=spawned.append,
    )
    tool_ids = ta.created[0]["kwargs"].get("tool_ids")
    assert tool_ids and "goal" in tool_ids
    for c in spawned: c.close()


@pytest.mark.asyncio
async def test_non_owner_task_session_keeps_default_toolset(monkeypatch):
    """A non-owner sender must NOT get the goal/twitter toolset (tenant least-privilege);
    tool_ids override is None so the conservative SessionRequest default stands."""
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    ta = _FakeTaskAgent()
    spawned = []
    await act_on_inbound(
        ta, _result(RouteKind.TASK_AGENT, text="hi", user="u_stranger"),
        spawn=spawned.append,
    )
    assert ta.created[0]["kwargs"].get("tool_ids") is None
    for c in spawned: c.close()


# --- 004: interactive reply delivery (run_session return is a STATUS string, not the
#          agent's answer — must extract the real reply and send it, exactly once) -----

@pytest.mark.asyncio
async def test_run_and_deliver_sends_extracted_reply_not_status_string():
    """THE 004 FIX: after run_session, deliver the agent's REAL reply (via
    _extract_chat_reply) — NOT run_session's 'Session completed successfully' status."""
    from surfaces.telegram.harness import _run_and_deliver
    ta = _FakeTaskAgent(deliver_ok=True)
    ta.reply = "Here's what I ran today."
    sent = []
    async def deliver(text): sent.append(text)
    await _run_and_deliver(ta, "u_abc", "sess_1", deliver)
    assert ta.ran == [("u_abc", "sess_1")]          # run_session was actually awaited
    assert sent == ["Here's what I ran today."]      # exactly ONE send of the real reply
    assert "Session completed successfully" not in sent  # never the status string


@pytest.mark.asyncio
async def test_run_and_deliver_no_send_when_reply_empty():
    """An empty extracted reply (agent produced nothing deliverable) sends nothing —
    never an empty/blank Telegram message."""
    from surfaces.telegram.harness import _run_and_deliver
    ta = _FakeTaskAgent(deliver_ok=True)
    ta.reply = ""
    sent = []
    async def deliver(text): sent.append(text)
    await _run_and_deliver(ta, "u", "s", deliver)
    assert ta.ran == [("u", "s")]
    assert sent == []


@pytest.mark.asyncio
async def test_run_and_deliver_tolerates_none_deliver():
    """deliver=None (no surface to send to) still runs the session; just no delivery."""
    from surfaces.telegram.harness import _run_and_deliver
    ta = _FakeTaskAgent(deliver_ok=True)
    ta.reply = "answer"
    await _run_and_deliver(ta, "u", "s", None)   # must not raise
    assert ta.ran == [("u", "s")]


@pytest.mark.asyncio
async def test_steer_delivered_path_delivers_agent_reply():
    """act_on_inbound threads `deliver` into the spawned run so a STEER resume actually
    answers the owner (awaiting the spawned coro delivers the extracted reply once)."""
    ta = _FakeTaskAgent(deliver_ok=True)
    ta.reply = "resumed answer"
    sent = []
    async def deliver(text): sent.append(text)
    spawned = []
    await act_on_inbound(ta, _result(RouteKind.STEER, text="go on", session_id="s1"),
                         spawn=spawned.append, deliver=deliver)
    assert len(spawned) == 1
    await spawned[0]                     # run the spawned coroutine
    assert sent == ["resumed answer"]    # the reply reached the chat


@pytest.mark.asyncio
async def test_fresh_task_session_delivers_agent_reply():
    """A fresh (TASK_AGENT) session also delivers its reply through the spawned run."""
    ta = _FakeTaskAgent()
    ta.reply = "fresh answer"
    sent = []
    async def deliver(text): sent.append(text)
    spawned = []
    await act_on_inbound(ta, _result(RouteKind.TASK_AGENT, text="do a thing"),
                         spawn=spawned.append, deliver=deliver)
    assert len(spawned) == 1
    await spawned[0]
    assert sent == ["fresh answer"]


@pytest.mark.asyncio
async def test_steer_touches_binding_on_successful_delivery():
    """a1: last-activity is bumped on delivery success (not in the router)."""
    ta = _FakeTaskAgent(deliver_ok=True)
    spawned = []
    await act_on_inbound(ta, _result(RouteKind.STEER, text="hi", session_id="s1"),
                         spawn=spawned.append)
    assert getattr(ta, "touched", []) == ["agent:main:telegram:dm:555:u_abc"]
    for c in spawned: c.close()


@pytest.mark.asyncio
async def test_steer_does_not_touch_when_not_delivered():
    """No touch when delivery fails (truly gone) — that path rebinds via create_session."""
    ta = _FakeTaskAgent(deliver_ok=False)
    spawned = []
    await act_on_inbound(ta, _result(RouteKind.STEER, text="hi", session_id="gone"),
                         spawn=spawned.append)
    assert getattr(ta, "touched", []) == []
    for c in spawned: c.close()


@pytest.mark.asyncio
async def test_steer_truly_gone_falls_back_to_fresh_session():
    """If the bound session is truly gone (no on-disk metadata -> deliver returns False),
    STEER falls back to a fresh session — never drops the message."""
    ta = _FakeTaskAgent(deliver_ok=False)
    spawned = []
    await act_on_inbound(ta, _result(RouteKind.STEER, text="continue", session_id="gone"),
                         spawn=spawned.append)
    assert ta.delivered and ta.delivered[0]["session_id"] == "gone"   # tried to resume first
    assert len(ta.created) == 1                                        # fell back to fresh
    assert ta.created[0]["kwargs"]["chat_session_key"] == "agent:main:telegram:dm:555:u_abc"
    assert len(spawned) == 1
    for c in spawned: c.close()


@pytest.mark.asyncio
async def test_warm_but_dead_resumes_bound_session_not_fresh():
    """THE FIX (P0.2): an evicted-but-recreatable session is resumed by its ORIGINAL
    session_id (restoring history from disk), NOT replaced by a new amnesiac session."""
    ta = _FakeTaskAgent(deliver_ok=True)  # recreatable from disk
    spawned = []
    await act_on_inbound(ta, _result(RouteKind.STEER, text="where were we?", session_id="evicted"),
                         spawn=spawned.append)
    assert ta.delivered[0]["session_id"] == "evicted"   # resumed the bound id
    assert ta.created == []                              # NO new session minted (the bug)
    assert len(spawned) == 1
    for c in spawned: c.close()


@pytest.mark.asyncio
async def test_steer_busy_replies_working_and_does_not_create_or_run():
    """a-MED2: a 'busy' (queue-full) session is ALIVE — touch it, tell the user we're
    still working, but never mint a fresh session nor double-spawn run_session."""
    ta = _FakeTaskAgent(deliver_ok="busy")
    spawned = []
    out = await act_on_inbound(ta, _result(RouteKind.STEER, text="and another thing", session_id="s1"),
                               spawn=spawned.append)
    assert isinstance(out, str) and out                    # a user-facing "still working" ack
    assert ta.created == []                                 # NO fresh session
    assert spawned == []                                    # NO second run spawned
    assert getattr(ta, "touched", []) == ["agent:main:telegram:dm:555:u_abc"]  # active -> touched


@pytest.mark.asyncio
async def test_command_cancel_cancels_bound_session():
    ta = _FakeTaskAgent()
    out = await act_on_inbound(ta, _result(RouteKind.COMMAND, command="/cancel", session_id="sess_1"),
                               spawn=lambda c: None)
    assert ta.cancelled == ["sess_1"]
    assert isinstance(out, str)  # a user-facing ack


@pytest.mark.asyncio
async def test_command_new_unbinds_chat_so_next_message_starts_fresh():
    """a4: /new cancels the bound session AND drops its chat binding, so the next
    message routes cold (a fresh session) instead of STEERing back into the old one."""
    ta = _FakeTaskAgent()
    out = await act_on_inbound(ta, _result(RouteKind.COMMAND, command="/new", session_id="sess_1"),
                               spawn=lambda c: None)
    assert ta.cancelled == ["sess_1"]
    assert getattr(ta, "unbound", []) == ["agent:main:telegram:dm:555:u_abc"]
    assert isinstance(out, str)


@pytest.mark.asyncio
async def test_command_help_returns_text():
    ta = _FakeTaskAgent()
    out = await act_on_inbound(ta, _result(RouteKind.COMMAND, command="/help"), spawn=lambda c: None)
    assert out and "/task" in out


@pytest.mark.asyncio
async def test_denied_does_not_start_session_and_returns_message():
    """A DENIED decision (pairing required / not allowed) must NOT run the agent.

    Pre-fix this fell through to _start_task_session, running the agent on a
    blocked message. The guard returns a user-facing message instead.
    """
    ta = _FakeTaskAgent()
    spawned = []
    decision = RouteDecision(RouteKind.DENIED, "agent:main:telegram:dm:555:u_abc",
                             pairing_code="ABC123")
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(text="let me in",
                             identity=Identity(user_id="u_abc", source=src, raw_user_id="555"))
    out = await act_on_inbound(ta, InboundResult(inbound=inbound, decision=decision),
                               spawn=spawned.append)
    assert ta.created == []          # agent NOT started
    assert spawned == []             # nothing spawned
    assert isinstance(out, str) and out  # a user-facing message
    assert "ABC123" in out           # surfaces the pairing code


# --- owner_allowed (ALLOWED_TELEGRAM_USER_IDS gate) --------------------------

def test_owner_allowed_empty_allowlist_returns_none(monkeypatch):
    """No allowlist set -> None ('no allowlist; bootstrap mode')."""
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    assert owner_allowed("12345") is None


def test_owner_allowed_blank_allowlist_returns_none(monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "  ")
    assert owner_allowed("12345") is None


def test_owner_allowed_listed_id_true(monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111, 12345 ,999")
    assert owner_allowed("12345") is True
    assert owner_allowed(12345) is True  # int-or-str tolerant


def test_owner_allowed_unlisted_id_false(monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "111,999")
    assert owner_allowed("12345") is False


# --- TelegramHarness: handle_update gate + run_polling ----------------------

class _PollBot:
    """Fake aiogram Bot: records sends/offsets, serves queued update batches."""
    def __init__(self):
        self.batches = []        # list[list[dict]] — one batch per get_updates call
        self.offsets = []        # offset arg seen on each get_updates call
        self.sent = []           # (chat_id, text)
        self.actions = []        # (chat_id, action) from send_chat_action
        self.deleted_webhook = False
        self.harness = None      # set after construction so the bot can stop the loop

    async def send_chat_action(self, chat_id, action):
        self.actions.append((str(chat_id), action))

    async def get_updates(self, offset=None, timeout=0):
        self.offsets.append(offset)
        if self.batches:
            return self.batches.pop(0)
        if self.harness is not None:
            self.harness._running = False  # drained -> let run_polling exit
        return []

    async def send_message(self, chat_id, text):
        self.sent.append((str(chat_id), text))
        return type("M", (), {"message_id": 1})()

    async def delete_webhook(self, **kw):
        self.deleted_webhook = True


class _FakeContainer:
    def get_service(self, name):
        return None
    def register_service(self, name, svc):
        pass


class _FakeUD:
    def resolve_internal(self, tg_id, source):
        return "u_" + str(tg_id)


class _FakeDedup:
    def __init__(self):
        self.seen_ids = set()
    def seen(self, update_id, now=None):
        if update_id in self.seen_ids:
            return True
        self.seen_ids.add(update_id)
        return False


def _upd(uid=1, from_id=12345, chat_id=555, text="hi"):
    return {
        "update_id": uid,
        "message": {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": from_id, "username": "me"},
            "text": text,
        },
    }


def _harness(bot, task_agent):
    return TelegramHarness(
        bot, _FakeContainer(), task_agent,
        webhook_base=None, dedup=_FakeDedup(), user_directory=_FakeUD(),
    )


@pytest.mark.asyncio
async def test_handle_update_empty_allowlist_replies_id_and_skips_agent(monkeypatch):
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    bot = _PollBot()
    ta = _FakeTaskAgent()
    h = _harness(bot, ta)
    await h.handle_update(_upd(from_id=12345))
    assert ta.created == []                       # agent NOT run
    assert bot.sent and "12345" in bot.sent[0][1]  # bootstrap reply reveals the id


@pytest.mark.asyncio
async def test_handle_update_non_owner_ignored(monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "999")
    bot = _PollBot()
    ta = _FakeTaskAgent()
    h = _harness(bot, ta)
    await h.handle_update(_upd(from_id=12345))
    assert ta.created == [] and bot.sent == []     # silently dropped


@pytest.mark.asyncio
async def test_handle_update_owner_routes_to_task_agent(monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "12345")
    bot = _PollBot()
    ta = _FakeTaskAgent()
    h = _harness(bot, ta)
    await h.handle_update(_upd(from_id=12345, text="scrape something"))
    assert len(ta.created) == 1
    assert ta.created[0]["request"] == "scrape something"
    assert ta.created[0]["kwargs"]["session_source"].surface_id == "telegram"


@pytest.mark.asyncio
async def test_handle_update_owner_sends_typing_indicator(monkeypatch):
    """An owner agent-turn fires a Telegram 'typing' action so the user sees the bot
    working during the (often long) LLM call."""
    import asyncio
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "12345")
    bot = _PollBot()
    ta = _FakeTaskAgent()
    h = _harness(bot, ta)
    h.typing_interval = 0.01
    await h.handle_update(_upd(from_id=12345, chat_id=555))
    await asyncio.sleep(0.05)  # let the spawned turn + typing keep-alive run
    assert any(a == ("555", "typing") for a in bot.actions)


@pytest.mark.asyncio
async def test_run_polling_drains_batches_and_advances_offset(monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "12345")
    bot = _PollBot()
    ta = _FakeTaskAgent()
    h = _harness(bot, ta)
    bot.harness = h
    bot.batches = [[_upd(uid=10), _upd(uid=11)]]
    await h.run_polling()
    assert len(ta.created) == 2          # both updates processed
    assert 12 in bot.offsets             # offset advanced past the last update_id


@pytest.mark.asyncio
async def test_build_telegram_harness_injected_bot_polling_mode():
    bot = _PollBot()
    container = _FakeContainer()
    ta = _FakeTaskAgent()
    h = build_telegram_harness(container, ta, token="x", webhook_base=None, bot=bot)
    assert isinstance(h, TelegramHarness)
    assert h.surface.surface_id == "telegram"


# --- long-reply chunking: a reply over Telegram's ~4096-char cap used to raise
#     TelegramBadRequest("message is too long"), caught fail-open, and the owner got
#     NOTHING (2026-07-03 live incident: session 2cd65776). Must split + send in order. --

def test_split_for_telegram_short_text_is_one_chunk():
    from surfaces.telegram.harness import _split_for_telegram
    assert _split_for_telegram("hello") == ["hello"]


def test_split_for_telegram_splits_long_text_under_limit_each():
    from surfaces.telegram.harness import _split_for_telegram, TELEGRAM_MAX_MESSAGE_LEN
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = _split_for_telegram(text, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    # no content lost/reordered
    assert "\n".join(chunks) == text


def test_split_for_telegram_hard_cuts_a_single_oversized_line():
    from surfaces.telegram.harness import _split_for_telegram
    text = "x" * 9000
    chunks = _split_for_telegram(text, limit=4096)
    assert len(chunks) == 3
    assert "".join(chunks) == text
    assert all(len(c) <= 4096 for c in chunks)


@pytest.mark.asyncio
async def test_send_telegram_text_sends_single_message_when_short():
    from surfaces.telegram.harness import _send_telegram_text
    sent = []
    class _Bot:
        async def send_message(self, chat_id, text, **kw): sent.append((chat_id, text))
    await _send_telegram_text(_Bot(), 555, "hi there")
    assert sent == [(555, "hi there")]


@pytest.mark.asyncio
async def test_send_telegram_text_splits_long_reply_into_multiple_sends():
    """THE FIX: a reply over the cap must arrive as several send_message calls, in
    order, reassembling to the original text — never raise / never drop silently."""
    from surfaces.telegram.harness import _send_telegram_text
    sent = []
    class _Bot:
        async def send_message(self, chat_id, text, **kw): sent.append((chat_id, text))
    long_reply = "\n".join(f"paragraph {i}: " + "y" * 100 for i in range(100))
    # _send_telegram_text has no limit= param; exercise via the module-level cap
    import surfaces.telegram.harness as h
    orig_limit = h.TELEGRAM_MAX_MESSAGE_LEN
    h.TELEGRAM_MAX_MESSAGE_LEN = 500
    try:
        await _send_telegram_text(_Bot(), 555, long_reply)
    finally:
        h.TELEGRAM_MAX_MESSAGE_LEN = orig_limit
    assert len(sent) > 1
    assert all(chat == 555 for chat, _ in sent)
    assert all(len(text) <= 500 for _, text in sent)
    assert "\n".join(text for _, text in sent) == long_reply


@pytest.mark.asyncio
async def test_telegram_bot_sink_splits_long_out_of_band_message():
    """Out-of-band deliveries (cron digest / goal / self-wake) go through
    TelegramBotSink — must ALSO chunk, since a long digest hits the same cap."""
    from surfaces.telegram.harness import TelegramBotSink
    import surfaces.telegram.harness as h
    sent = []
    class _Bot:
        async def send_message(self, chat_id, text, **kw): sent.append((chat_id, text))
    orig_limit = h.TELEGRAM_MAX_MESSAGE_LEN
    h.TELEGRAM_MAX_MESSAGE_LEN = 500
    try:
        sink = TelegramBotSink(_Bot())
        long_text = "\n".join(f"update {i}" * 20 for i in range(50))
        ok = await sink.send_message("555", long_text)
    finally:
        h.TELEGRAM_MAX_MESSAGE_LEN = orig_limit
    assert ok is True
    assert len(sent) > 1
    assert all(len(text) <= 500 for _, text in sent)
