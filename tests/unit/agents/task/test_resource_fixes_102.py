"""102 Phase 1: per-step waste and stall sources on the event loop."""
import asyncio
import gc
import json
import logging
import queue
import time
import types
from pathlib import Path

import pytest

from agents.task.agent.core import live_health


# --- C-2: gc.freeze at the entries, the collect moves to the eviction batch ------

def test_freeze_startup_heap_runs_once_per_process(monkeypatch):
    from polyrob.app import bootstrap

    gc.unfreeze()
    monkeypatch.setattr(bootstrap, "_startup_heap_frozen", False)
    try:
        bootstrap.freeze_startup_heap()
        count = gc.get_freeze_count()
        assert count > 0
        bootstrap.freeze_startup_heap()
        assert gc.get_freeze_count() == count
    finally:
        gc.unfreeze()


def test_every_process_entry_freezes():
    root = Path(__file__).resolve().parents[4]
    for rel in ("api/app.py", "cli/commands/gateway.py", "cli/commands/_surface_runner.py",
                "cli/commands/chat.py"):
        assert "freeze_startup_heap()" in (root / rel).read_text(), rel


def test_a_run_end_has_no_collect_and_an_eviction_batch_has_one():
    root = Path(__file__).resolve().parents[4]
    assert "gc.collect" not in (root / "agents/task/agent/core/run_loop.py").read_text()

    from agents.task.task_agent_lifecycle import TaskAgentLifecycleMixin as Mixin
    host = types.SimpleNamespace(
        session_ttl_seconds=1, max_sessions_in_memory=10,
        _session_last_activity={"old": 0.0}, _registry=types.SimpleNamespace(count=lambda: 1))

    async def _stale():
        return None

    async def _evict(session_id, reason):
        host._session_last_activity.pop(session_id)

    host._cleanup_stale_created_sessions = _stale
    host._evict_session = _evict
    collected = []
    real = gc.collect
    gc.collect = lambda *a: collected.append(1) or 0
    try:
        asyncio.run(Mixin._cleanup_old_sessions(host))
        assert collected == [1]
        collected.clear()
        asyncio.run(Mixin._cleanup_old_sessions(host))
        assert collected == []
    finally:
        gc.collect = real


# --- N-2: one client, a hung console does not hold the stream ---------------------

def test_stream_chunks_share_one_client_keep_order_and_never_wait(monkeypatch):
    import httpx
    from agents.task import utils_webview

    built, posted = [], []
    release = None

    class _Client:
        def __init__(self, **kw):
            built.append(kw)

        async def post(self, endpoint, json, timeout):
            posted.append(json["chunk"])
            await release.wait()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def go():
        nonlocal release
        release = asyncio.Event()
        cb = utils_webview.make_webview_stream_callback()
        began = time.monotonic()
        for i in range(5):
            await cb("s", "a", str(i), 1)
        spent = time.monotonic() - began
        await asyncio.sleep(0)
        release.set()
        await asyncio.sleep(0.01)
        return spent

    assert asyncio.run(go()) < 0.05
    assert len(built) == 1 and built[0]["verify"] is False
    assert posted == ["0", "1", "2", "3", "4"]


def test_a_remote_stream_target_keeps_the_certificate_check(monkeypatch):
    import httpx
    from agents.task import utils_webview

    built = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: built.append(kw))

    async def go():
        utils_webview._stream_client(asyncio.get_running_loop(), "https://console.example")

    asyncio.run(go())
    assert built[0]["verify"] is True


def test_a_full_stream_queue_drops_the_chunk(monkeypatch):
    from agents.task import utils_webview

    monkeypatch.setattr(utils_webview, "_STREAM_QUEUE_MAX", 1)
    monkeypatch.setattr(utils_webview, "_stream_client", lambda *a: types.SimpleNamespace(
        post=lambda *x, **k: asyncio.sleep(0)))

    async def go():
        cb = utils_webview.make_webview_stream_callback()
        await cb("s", "a", "first", 1)
        await cb("s", "a", "dropped", 1)  # no raise, no wait
        await asyncio.sleep(0.01)

    asyncio.run(go())


# --- C-5: the feed post leaves the caller's thread --------------------------------

def test_a_hung_console_does_not_block_the_feed_emit(monkeypatch):
    from agents.task.telemetry import service

    monkeypatch.setattr(service, "_feed_posts", queue.Queue(maxsize=2))
    monkeypatch.setattr(service, "_feed_poster", object())  # no thread in this test
    began = time.monotonic()
    for i in range(5):
        service._queue_feed_post("http://127.0.0.1:1/x", {"n": i}, "t")
    assert time.monotonic() - began < 0.005
    assert service._feed_posts.qsize() == 2


# --- C-1: snapshot TTL ------------------------------------------------------------

class _Health(live_health.LiveHealthMixin):
    def __init__(self, n_steps):
        self.orchestrator = None
        self.controller = None
        self.user_id = "u"
        self.session_id = "s"
        self.state = types.SimpleNamespace(n_steps=n_steps)


def test_three_steps_in_a_second_build_one_snapshot(monkeypatch):
    builds = []
    monkeypatch.setattr(live_health, "live_health_injected", lambda a: True)
    monkeypatch.setattr(live_health, "has_status_tool", lambda c: False)
    monkeypatch.setattr(live_health, "_operations_injected", lambda a: False)
    monkeypatch.setattr(live_health, "build_live_health_text",
                        lambda *a, **k: builds.append(1) or f"t{len(builds)}")
    agent = _Health(n_steps=2)
    texts = [asyncio.run(agent._live_health_part()) for _ in range(3)]
    assert texts == ["t1", "t1", "t1"] and len(builds) == 1

    agent._live_health_cache = (time.monotonic() - live_health.LIVE_HEALTH_TTL_S - 1, "t1")
    assert asyncio.run(agent._live_health_part()) == "t2"

    agent.state.n_steps = 1  # a first step is always fresh
    assert asyncio.run(agent._live_health_part()) == "t3"


def test_a_failed_build_is_not_cached(monkeypatch):
    monkeypatch.setattr(live_health, "live_health_injected", lambda a: True)
    monkeypatch.setattr(live_health, "has_status_tool", lambda c: False)
    monkeypatch.setattr(live_health, "_operations_injected", lambda a: False)

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(live_health, "build_live_health_text", boom)
    agent = _Health(n_steps=2)
    assert "unavailable" in asyncio.run(agent._live_health_part())
    assert getattr(agent, "_live_health_cache", None) is None


# --- C-10: the history write is atomic --------------------------------------------

def test_a_failed_dump_leaves_the_old_history_readable(tmp_path, monkeypatch):
    from agents.task.agent.messages import persistence

    target = tmp_path / "memory" / "message_history.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"keep": True}))
    target.chmod(0o640)

    class _PM:
        def create_file_path(self, **kw):
            return target

    monkeypatch.setattr("agents.task.path.pm", lambda: _PM())
    monkeypatch.setattr("agents.task.agent.messages.foundation_replay.capture_foundation",
                        lambda host: {})
    from agents.task.agent.message_manager.views import MessageHistory

    class _Host(persistence.PersistenceMixin):
        pass

    host = _Host()
    host.history = MessageHistory()
    host.logger = logging.getLogger("test.persistence")
    host.tool_id = 1

    real_dump = json.dump

    def torn(obj, fh, **kw):
        fh.write('{"torn"')
        raise OSError("no space left")

    monkeypatch.setattr(persistence.json, "dump", torn)
    host.save_to_disk("s", "u")
    assert json.loads(target.read_text()) == {"keep": True}
    assert [p.name for p in target.parent.iterdir()] == [target.name]

    monkeypatch.setattr(persistence.json, "dump", real_dump)
    host.save_to_disk("s", "u")
    assert "keep" not in json.loads(target.read_text())
    assert target.stat().st_mode & 0o777 == 0o640


# --- C-11a + C-3 ------------------------------------------------------------------

def test_an_unknown_model_warns_once(caplog):
    from modules.llm.model_registry import ModelRegistry

    reg = ModelRegistry()
    with caplog.at_level(logging.WARNING, logger="modules.llm.model_registry"):
        for _ in range(3):
            reg.get_model("no-such-model-id")
    assert sum("no catalog row" in r.getMessage() for r in caplog.records) == 1
