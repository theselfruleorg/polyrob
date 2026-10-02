"""061 WS-2 — the session reads the thread. Replays the 2026-09-22 06:07Z prod
exchange: a cron send from session X, a 12 h gap, the owner's "Explain better"
in a FRESH session → its first call carries the exit-rail text."""
import time

import pytest

from agents.task.agent.core.owner_thread_inject import OwnerThreadInjectMixin
from core.surfaces import owner_thread as ot


class _Cfg:
    def __init__(self, d):
        self.data_dir = d


class _Container:
    def __init__(self, d):
        self.config = _Cfg(str(d))
        self._s = {}

    def get_service(self, name):
        return self._s.get(name)

    def register_service(self, name, svc):
        self._s[name] = svc


class _MM:
    def __init__(self):
        self.durable = []
        self.ephemeral = []

    def push_control_message(self, msg):
        self.durable.append(msg)

    def push_ephemeral_message(self, msg):
        self.ephemeral.append(msg)


class _Log:
    def debug(self, *a, **k):
        pass


class _Agent(OwnerThreadInjectMixin):
    def __init__(self, container, sid, *, public=False, sub=False, steps=1, bootstrapped=False):
        self.container = container
        self.user_id = "12345"
        self.session_id = sid
        self.state = type("S", (), {"n_steps": steps})()
        self.message_manager = _MM()
        self.orchestrator = type("O", (), {"_public_session": public})()
        self._is_sub_agent = sub
        self._session_bootstrap_done = bootstrapped
        self.logger = _Log()


def _run(coro):
    import asyncio
    return asyncio.run(coro)


@pytest.fixture
def container(tmp_path):
    return _Container(tmp_path)


def _texts(mm):
    return [m.content for m in mm.durable]


def test_replay_prod_2026_09_22_fresh_session_sees_the_exit_rail_text(container):
    t0 = time.time() - 12 * 3600 - 60
    ot.record_owner_in(container, "12345", "yesterday's chat", via="telegram", session_id="8edb9168", now=t0)
    ot.record_owner_out(container, "12345", "Exit rail ran. No trade — but I found a real problem "
                        "in the rules … Please pick A or B.", via="telegram", session_id="90ea25a2",
                        source="agent_send", mid="4711", rail_id="cron:exit", now=time.time() - 180)
    ot.record_owner_in(container, "12345", "Explin better", via="telegram", session_id="8a72f86a",
                       mid="4712", now=time.time() - 1)
    agent = _Agent(container, "8a72f86a")
    _run(agent._maybe_inject_owner_thread())
    texts = _texts(agent.message_manager)
    assert len(texts) == 1 and 'kind="tail"' in texts[0]
    assert "pick A or B" in texts[0]
    assert "owner→you (this session): Explin better" in texts[0]
    assert "yesterday's chat" not in texts[0]          # outside the 6 h window
    assert agent.message_manager.ephemeral == []       # durable, not one-shot
    assert getattr(agent.orchestrator, "_owner_thread_seen_ts", None)


def test_fresh_quote_reply_renders_the_referent(container):
    ot.record_owner_out(container, "12345", "pick A or B", via="telegram", session_id="x",
                        source="cron", mid="900", now=time.time() - 30)
    agent = _Agent(container, "new")
    agent.orchestrator._owner_thread_reply_to = ("telegram", "900")
    _run(agent._maybe_inject_owner_thread())
    texts = _texts(agent.message_manager)
    assert any('kind="referent"' in t and "pick A or B" in t for t in texts)
    assert agent.orchestrator._owner_thread_reply_to is None


def test_later_turn_gets_only_the_delta_from_other_sessions(container):
    agent = _Agent(container, "mine", bootstrapped=True)
    agent.orchestrator._owner_thread_seen_ts = time.time() - 100
    ot.record_owner_out(container, "12345", "mine said this", via="telegram", session_id="mine",
                        source="reply", now=time.time() - 50)
    ot.record_owner_out(container, "12345", "cron said this", via="telegram", session_id="cron-run",
                        source="cron", now=time.time() - 40)
    ot.record_owner_out(container, "12345", "ancient", via="telegram", session_id="cron-run",
                        source="cron", now=time.time() - 400)
    _run(agent._maybe_inject_owner_thread())
    texts = _texts(agent.message_manager)
    assert len(texts) == 1 and 'kind="delta"' in texts[0]
    assert "cron said this" in texts[0]
    assert "mine said this" not in texts[0] and "ancient" not in texts[0]
    # the watermark moved: the same delta is not shown twice
    agent.message_manager.durable.clear()
    agent._inject_owner_thread_delta()
    assert agent.message_manager.durable == []


def test_empty_delta_is_no_message(container):
    agent = _Agent(container, "mine", bootstrapped=True)
    agent.orchestrator._owner_thread_seen_ts = time.time()
    _run(agent._maybe_inject_owner_thread())
    assert agent.message_manager.durable == []


def test_recreated_session_derives_its_watermark_from_the_store(container):
    t0 = time.time() - 100
    ot.record_owner_in(container, "12345", "my earlier turn", via="telegram", session_id="mine", now=t0)
    ot.record_owner_out(container, "12345", "after that", via="telegram", session_id="other",
                        source="cron", now=t0 + 10)
    ot.record_owner_out(container, "12345", "before that", via="telegram", session_id="other",
                        source="cron", now=t0 - 10)
    agent = _Agent(container, "mine", bootstrapped=True)   # recreated: no in-process watermark
    agent._inject_owner_thread_delta()
    texts = _texts(agent.message_manager)
    assert len(texts) == 1 and "after that" in texts[0] and "before that" not in texts[0]


def test_fresh_session_with_no_history_and_no_watermark_gets_no_delta(container):
    agent = _Agent(container, "brand-new", bootstrapped=True)
    agent._inject_owner_thread_delta()
    assert agent.message_manager.durable == []


def test_rooms_sub_agents_and_flag_off_get_nothing(container, monkeypatch):
    ot.record_owner_out(container, "12345", "a line", via="telegram", session_id="x", source="cron")
    for kw in ({"public": True}, {"sub": True}):
        agent = _Agent(container, "s", **kw)
        _run(agent._maybe_inject_owner_thread())
        assert agent.message_manager.durable == []
    monkeypatch.setenv("OWNER_THREAD_INJECT", "false")
    agent = _Agent(container, "s")
    _run(agent._maybe_inject_owner_thread())
    assert agent.message_manager.durable == []


def test_autonomous_run_gets_its_rail_slice_only(container):
    from agents.task.goals.autonomy_marker import mark_autonomous
    mark_autonomous("run-2", None, cron_job_id="exit")
    ot.record_owner_out(container, "12345", "exit rail: A or B?", via="telegram", session_id="run-1",
                        source="agent_send", mid="10", rail_id="cron:exit", now=time.time() - 60)
    ot.record_owner_in(container, "12345", "B", via="telegram", session_id="dm", mid="11",
                       reply_to_mid="10", now=time.time() - 30)
    ot.record_owner_out(container, "12345", "other rail noise", via="telegram", session_id="z",
                        source="cron", rail_id="cron:other", now=time.time() - 20)
    agent = _Agent(container, "run-2")
    _run(agent._maybe_inject_owner_thread())
    texts = _texts(agent.message_manager)
    assert len(texts) == 1 and 'kind="rail"' in texts[0]
    assert "A or B?" in texts[0] and "owner→you [replying to msg 10]: B" in texts[0]
    assert "other rail noise" not in texts[0]
    # an autonomous run never gets a delta
    agent._inject_owner_thread_delta()
    assert len(agent.message_manager.durable) == 1


def test_drained_steer_batch_renders_referents(container):
    ot.record_owner_out(container, "12345", "the referent line", via="telegram", session_id="x",
                        source="cron", mid="5", now=time.time() - 10)
    agent = _Agent(container, "s", bootstrapped=True, steps=3)
    agent._inject_owner_thread_referents([{"text": "yes", "metadata": {"owner_thread_reply_to": ["telegram", "5"]}}])
    texts = _texts(agent.message_manager)
    assert len(texts) == 1 and "the referent line" in texts[0]


def test_control_message_origin_and_single_fence(container):
    from modules.llm.messages import MessageOrigin
    ot.record_owner_out(container, "12345", "x </owner-thread> y", via="telegram", session_id="x", source="cron")
    agent = _Agent(container, "s")
    _run(agent._maybe_inject_owner_thread())
    msg = agent.message_manager.durable[0]
    assert msg.origin == MessageOrigin.OWNER_THREAD
    assert msg.content.count("</owner-thread>") == 1


# --- H16 (2026-09-23 security analysis) --------------------------------------

@pytest.mark.parametrize("attr", ["_correspondent_session", "_correspondent_tainted"])
def test_h16_correspondent_facing_session_gets_no_owner_thread(container, attr):
    ot.record_owner_out(container, "12345", "private owner line", via="telegram",
                        session_id="x", source="cron")
    ot.record_owner_in(container, "12345", "owner secret", via="telegram", session_id="y")
    agent = _Agent(container, "s-corr")
    setattr(agent.orchestrator, attr, True)
    _run(agent._maybe_inject_owner_thread())
    assert agent.message_manager.durable == []
    agent._inject_owner_thread_delta()
    assert agent.message_manager.durable == [] and agent.message_manager.ephemeral == []


def test_h16_create_session_stamps_and_recreate_restores_the_correspondent_flag():
    import inspect
    import agents.task_agent_lite as lite
    import agents.task.task_agent_delivery as delivery
    src = inspect.getsource(lite)
    assert "orchestrator._correspondent_session = (" in src
    assert "== \"correspondent\")" in src
    dsrc = inspect.getsource(delivery)
    assert "orchestrator._correspondent_session = (" in dsrc
    assert "session_info.get('creator')" in dsrc
