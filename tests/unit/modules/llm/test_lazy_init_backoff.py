"""098 MO-13: an LLM provider's lazy init is never latched off until restart."""
import asyncio

import pytest

from core.config import BotConfig
from core.exceptions import LLMAuthenticationError
from modules.llm.init_gate import ProviderInitGate, note_call_error, recent_call_error
from modules.llm.llm_manager import LLMManager


class _Client:
    def __init__(self, fail):
        self.fail = fail
        self._initialized = False

    async def initialize(self):
        await asyncio.sleep(0)
        if self.fail:
            raise ConnectionError("dns")
        self._initialized = True


def _mgr(monkeypatch, fails, now):
    mgr = LLMManager(name="llm", config=BotConfig())
    mgr._initialized = True
    mgr._container = type("C", (), {"register_service": lambda self, *a, **k: None})()
    mgr.llm_config = {"openai": {"api_key": "k", "model": "gpt-5"}}
    mgr._init_gate = ProviderInitGate(base=30, cap=600, clock=lambda: now[0])
    built = []

    def _create(**kw):
        c = _Client(fails.pop(0) if fails else False)
        built.append(c)
        return c
    monkeypatch.setattr("modules.llm.llm_manager.create_llm_client", _create)
    monkeypatch.setattr(mgr, "_configure_client_token_limits", lambda c, m: None)
    return mgr, built


def test_transient_init_failure_retries_after_backoff(monkeypatch):
    async def go():
        now = [0.0]
        mgr, built = _mgr(monkeypatch, [True], now)
        assert await mgr._try_initialize_client("openai") is None
        assert await mgr._try_initialize_client("openai") is None  # inside backoff
        assert len(built) == 1
        now[0] = 30.0
        c = await mgr._try_initialize_client("openai")
        assert c is not None and mgr.clients["openai_client"] is c
    asyncio.run(go())


def test_key_added_after_first_attempt_is_used(monkeypatch):
    async def go():
        now = [0.0]
        mgr, built = _mgr(monkeypatch, [], now)
        mgr.llm_config = {"openai": {}}
        keys = {"openai": {}}
        monkeypatch.setattr(type(mgr.config), "get_llm_config", lambda self: keys)
        assert await mgr._try_initialize_client("openai") is None
        keys["openai"] = {"api_key": "k2", "model": "gpt-5"}
        now[0] = 31.0
        assert await mgr._try_initialize_client("openai") is not None
    asyncio.run(go())


def test_cleanup_resets_fallback_and_attempts(monkeypatch):
    async def go():
        now = [0.0]
        mgr, _ = _mgr(monkeypatch, [True], now)
        await mgr._try_initialize_client("openai")
        mgr.fallback_client_name = "openai_client"
        await mgr._cleanup()
        assert mgr.fallback_client_name is None
        assert mgr._init_gate.may_try("openai")
    asyncio.run(go())


def test_concurrent_first_calls_initialize_once(monkeypatch):
    async def go():
        now = [0.0]
        mgr, built = _mgr(monkeypatch, [], now)
        got = await asyncio.gather(*[mgr._try_initialize_client("openai") for _ in range(5)])
        assert len(built) == 1 and all(g is got[0] for g in got)
    asyncio.run(go())


def test_backoff_is_capped():
    now = [0.0]
    g = ProviderInitGate(base=30, cap=600, clock=lambda: now[0])
    for _ in range(10):
        g.failed("p")
    assert g.retry_in("p") == 600


def test_health_reads_real_call_errors(monkeypatch):
    async def go():
        mgr = LLMManager(name="llm", config=BotConfig())
        c = _Client(False)
        c._initialized = True
        assert await mgr._test_client_health(c)
        note_call_error(c, LLMAuthenticationError("401"))
        assert recent_call_error(c)
        assert not await mgr._test_client_health(c)
        d = _Client(False)
        d._initialized = True
        note_call_error(d, ValueError("not a provider fault"))
        assert await mgr._test_client_health(d)
    asyncio.run(go())
