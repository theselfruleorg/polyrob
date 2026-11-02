"""F5 (prod review 2026-10-29): owner-chat continuity across session boundaries.

A chat episode (the continuity bridge's source) was written only on TTL
eviction, so /new, the daily reset and restarts wrote none (5 episodes for ~17
owner sessions in 14 days). Its summary was the closing reply cut at 500 chars:
the whole 10-17/18 bounty-and-payments session became "Owner asked the time".

Now ONE method, ``SessionOrchestrator.write_chat_episode``, is called at every
boundary (eviction, /new, idle/daily reset, drained restart), and the summary
covers the whole session: decisions, standing instructions, open promises and
what was done — one bounded aux-LLM call, fail-open to a structured extract of
the owner's lines and the agent's replies.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import modules.memory.registry as reg
from agents.task.session import chat_episode as ce
from agents.task.session.cleanup import SessionCleanupMixin
from core.surfaces import owner_thread as ot
from core.surfaces.continuity import build_bridge_message
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


class _Container:
    def __init__(self, d):
        self.config = SimpleNamespace(data_dir=str(d))
        self._svc = {}

    def get_service(self, name):
        return self._svc.get(name)

    def register_service(self, name, svc):
        self._svc[name] = svc


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    monkeypatch.setenv("CONTINUITY_BRIDGE_ENABLED", "true")
    p = SqliteMemoryProvider(str(tmp_path / "memory.db"))
    reg.reset_memory_registry()
    reg.set_external_memory_provider(p)
    yield p
    reg.reset_memory_registry()


class _History:
    def __init__(self, steps, final=None):
        self.history = list(range(steps))
        self._final = final

    def final_result(self):
        return self._final

    def is_done(self):
        return False


class _Agent:
    def __init__(self, steps=3, final="Answered: it is 03:05 UTC.", llm=None):
        self.history = _History(steps, final)
        self.task = "chat with the owner"
        self._is_sub_agent = False
        self.task_context_manager = None
        self.message_manager = SimpleNamespace(aux_llm=llm)
        self.llm = llm


class _Orch(SessionCleanupMixin):
    def __init__(self, container, session_id="old-sess", agent=None):
        self.session_id = session_id
        self.user_id = "u1"
        self.container = container
        self.agents = {"main": agent or _Agent()}
        self._chat_session_key = "agent:main:telegram:dm:42"
        self._browser_contexts = set()
        self.browser_manager = None
        self.controller = None
        self.session_manager = None
        self.logger = MagicMock()


def _seed_thread(container, session_id="old-sess"):
    now = time.time() - 3600
    lines = [
        ("in", "we pay the bounty in PNL, not USDG — that is the rule from now on", "", "comment"),
        ("out", "Noted: payouts in PNL from now on.", "reply", ""),
        ("in", "judge round 2 on 10-31 after the 48 h window", "", "comment"),
        ("out", "I will judge round 2 on 10-31 and raise the payout cards.", "reply", ""),
        ("out", "SEND RESULT " + "x" * 500, "owner_verb", ""),
        ("in", "what time is it?", "", "comment"),
        ("out", "Answered: it is 03:05 UTC.", "reply", ""),
    ]
    for i, (d, body, src, kind) in enumerate(lines):
        if d == "in":
            ot.record_owner_in(container, "u1", body, via="telegram", session_id=session_id,
                               kind=kind, now=now + i)
        else:
            ot.record_owner_out(container, "u1", body, via="telegram", session_id=session_id,
                                source=src, rail_id="", now=now + i)


def test_the_structured_extract_covers_the_whole_session(tmp_path):
    c = _Container(tmp_path)
    _seed_thread(c)
    text = ce.structured_summary(ot.thread_for_session(c, "u1", "old-sess"))
    assert "PNL, not USDG" in text           # the standing rule, not only the last reply
    assert "judge round 2 on 10-31" in text  # the open promise
    assert "03:05 UTC" in text               # and the closing exchange
    assert "x" * 200 not in text             # a verb result is not the conversation
    assert len(text) <= ce.SESSION_SUMMARY_MAX_CHARS


class _LLM:
    def __init__(self, text="Decisions: pay in PNL.\nOpen promises: judge round 2 on 10-31."):
        self.calls, self.text = [], text

    async def ainvoke(self, messages, tools=None, **kw):
        self.calls.append(messages)
        return SimpleNamespace(content=self.text, usage_metadata=None)


@pytest.mark.asyncio
async def test_the_aux_summary_is_one_bounded_call_over_the_transcript(tmp_path):
    c = _Container(tmp_path)
    _seed_thread(c)
    llm = _LLM()
    out = await ce.summarize_session(_Orch(c, agent=_Agent(llm=llm)), use_llm=True)
    assert len(llm.calls) == 1
    prompt = llm.calls[0][0].content
    assert "PNL, not USDG" in prompt and "Open promises" in prompt
    assert out.startswith("Decisions: pay in PNL.")


@pytest.mark.asyncio
async def test_a_failing_aux_call_falls_back_to_the_extract(tmp_path):
    class _Boom:
        async def ainvoke(self, *a, **k):
            raise RuntimeError("provider down")

    c = _Container(tmp_path)
    _seed_thread(c)
    out = await ce.summarize_session(_Orch(c, agent=_Agent(llm=_Boom())), use_llm=True)
    assert "PNL, not USDG" in out


@pytest.mark.asyncio
async def test_with_no_thread_rows_the_closing_reply_still_counts(tmp_path):
    out = await ce.summarize_session(_Orch(_Container(tmp_path)), use_llm=False)
    assert "03:05 UTC" in (out or "")


@pytest.mark.asyncio
async def test_a_boundary_write_feeds_the_bridge_and_is_not_repeated(provider, tmp_path):
    c = _Container(tmp_path)
    _seed_thread(c)
    orch = _Orch(c)
    assert await orch.write_chat_episode(status="suspended", source="new", use_llm=False)
    msg = await build_bridge_message(user_id="u1", thread_key=orch._chat_session_key, owner=True)
    assert msg is not None and "PNL, not USDG" in msg.content
    # nothing new in the session since: the next boundary (eviction) writes nothing
    assert not await orch.write_chat_episode(status="suspended", source="session_end",
                                             use_llm=False)
    orch.agents["main"].history.history.append("one more step")
    assert await orch.write_chat_episode(status="suspended", source="session_end",
                                         use_llm=False)


@pytest.mark.asyncio
async def test_no_episode_for_a_room_or_an_autonomous_session(provider, tmp_path):
    from core.autonomy_marker import mark_autonomous
    c = _Container(tmp_path)
    room = _Orch(c, session_id="room-sess")
    room._public_session = True
    assert not await room.write_chat_episode(status="suspended", source="reset", use_llm=False)
    mark_autonomous("cron-sess", None, cron_job_id="j1")
    assert not await _Orch(c, session_id="cron-sess").write_chat_episode(
        status="suspended", source="reset", use_llm=False)


# --- the boundaries call it -------------------------------------------------

class _Registry:
    def __init__(self, rows):
        self.rows = dict(rows)

    def resolve(self, key):
        return self.rows.get(key)

    def delete(self, key):
        self.rows.pop(key, None)


def _task_agent(orch, row):
    from agents.task.task_agent_delivery import TaskAgentDeliveryMixin

    class _TA(TaskAgentDeliveryMixin):
        pass

    ta = _TA()
    registry = _Registry({orch._chat_session_key: row} if row else {})
    ta.container = SimpleNamespace(get_service=lambda n: registry if n == "session_chat_registry" else None)
    ta._registry = SimpleNamespace(get=lambda sid: orch if sid == orch.session_id else None,
                                   items=lambda: [(orch.session_id, orch)])
    return ta, registry


@pytest.mark.asyncio
async def test_slash_new_writes_the_outgoing_episode(provider, tmp_path):
    c = _Container(tmp_path)
    _seed_thread(c)
    orch = _Orch(c)
    ta, registry = _task_agent(orch, {"session_id": "old-sess"})
    ta.unbind_chat(orch._chat_session_key)
    assert registry.resolve(orch._chat_session_key) is None
    await ta.close_previous_chat_episode(orch._chat_session_key, "new-sess")   # awaits the write
    out = await reg.memory_recall_episodes(user_id="u1", kind="chat",
                                           thread_key=orch._chat_session_key, limit=1)
    assert out and "PNL, not USDG" in out[0].summary
    assert (out[0].meta or {}).get("source") == "new"


@pytest.mark.asyncio
async def test_a_reset_writes_the_outgoing_episode_before_the_new_session(provider, tmp_path):
    c = _Container(tmp_path)
    _seed_thread(c)
    orch = _Orch(c)
    ta, _ = _task_agent(orch, {"session_id": "old-sess"})
    await ta.close_previous_chat_episode(orch._chat_session_key, "new-sess")
    out = await reg.memory_recall_episodes(user_id="u1", kind="chat",
                                           thread_key=orch._chat_session_key, limit=1)
    assert out and (out[0].meta or {}).get("source") == "reset"


@pytest.mark.asyncio
async def test_the_same_session_rebinding_writes_nothing(provider, tmp_path):
    c = _Container(tmp_path)
    orch = _Orch(c)
    ta, _ = _task_agent(orch, {"session_id": "old-sess"})
    await ta.close_previous_chat_episode(orch._chat_session_key, "old-sess")
    out = await reg.memory_recall_episodes(user_id="u1", kind="chat",
                                           thread_key=orch._chat_session_key, limit=1)
    assert not out


@pytest.mark.asyncio
async def test_a_drained_restart_writes_every_resident_owner_chat(provider, tmp_path):
    c = _Container(tmp_path)
    _seed_thread(c)
    orch = _Orch(c)
    ta, _ = _task_agent(orch, None)
    assert await ta.write_open_chat_episodes(source="restart") == 1
    out = await reg.memory_recall_episodes(user_id="u1", kind="chat",
                                           thread_key=orch._chat_session_key, limit=1)
    assert out and (out[0].meta or {}).get("source") == "restart"


# --- review H3 / L2 / L3 ------------------------------------------------------

class _SlowLLM(_LLM):
    """An aux model that answers only when released (a 20 s call in prod)."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def ainvoke(self, messages, tools=None, **kw):
        self.calls.append(messages)
        await self.release.wait()
        return SimpleNamespace(content="LLM ACCOUNT: pay in PNL.", usage_metadata=None)


@pytest.mark.asyncio
async def test_the_intake_path_never_awaits_the_llm_summary(provider, tmp_path):
    """H3: create_session (Telegram intake) awaited a 25 s aux call."""
    c = _Container(tmp_path)
    _seed_thread(c)
    llm = _SlowLLM()
    orch = _Orch(c, agent=_Agent(llm=llm))
    ta, _ = _task_agent(orch, {"session_id": "old-sess"})
    ta.unbind_chat(orch._chat_session_key)
    # returns while the aux model is still blocked; the extract row is there
    await asyncio.wait_for(ta.close_previous_chat_episode(orch._chat_session_key, "new-sess"),
                           timeout=2)
    out = await reg.memory_recall_episodes(user_id="u1", kind="chat",
                                           thread_key=orch._chat_session_key, limit=1)
    assert out and "PNL, not USDG" in out[0].summary
    # the background upgrade replaces the row with the LLM account
    for _ in range(50):
        if llm.calls:
            break
        await asyncio.sleep(0.01)
    assert len(llm.calls) == 1
    llm.release.set()
    for _ in range(100):
        out = await reg.memory_recall_episodes(user_id="u1", kind="chat",
                                               thread_key=orch._chat_session_key, limit=1)
        if out and out[0].summary.startswith("LLM ACCOUNT"):
            break
        await asyncio.sleep(0.01)
    assert out[0].summary.startswith("LLM ACCOUNT")
    # and a later extract never downgrades it
    assert not await orch.write_chat_episode(status="suspended", source="session_end",
                                             use_llm=False)


@pytest.mark.asyncio
async def test_two_boundaries_at_once_make_one_llm_call(provider, tmp_path):
    """L3: /new + eviction both passed the mark check -> two summary calls."""
    c = _Container(tmp_path)
    _seed_thread(c)
    llm = _SlowLLM()
    orch = _Orch(c, agent=_Agent(llm=llm))
    first = asyncio.create_task(orch.write_chat_episode(status="suspended", source="new"))
    for _ in range(50):
        if llm.calls:
            break
        await asyncio.sleep(0.01)
    assert not await orch.write_chat_episode(status="suspended", source="session_end")
    llm.release.set()
    assert await first
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_shutdown_cleanup_writes_the_extract_without_the_llm(provider, tmp_path):
    """L2: TaskAgent._cleanup ran one aux call per session, in sequence."""
    c = _Container(tmp_path)
    _seed_thread(c)
    llm = _SlowLLM()
    orch = _Orch(c, agent=_Agent(llm=llm))
    orch._episode_extract_only = True
    calls = []

    async def _spy(**kw):
        calls.append(kw)
        return await SessionCleanupMixin.write_chat_episode(orch, **kw)

    orch.write_chat_episode = _spy
    orch._run_session_end_hooks = None
    orch.release_shell_sandbox = lambda: asyncio.sleep(0)
    try:
        await asyncio.wait_for(orch.cleanup(preserve_workspace=True, preserve_agents=False,
                                            full_cleanup=True), timeout=5)
    except (AttributeError, TypeError):
        pass   # teardown of the bare test double past the episode write
    assert calls and calls[0]["use_llm"] is False
    assert llm.calls == []
