"""UP-08 Step 8.5 — Gemini explicit cachedContents lifecycle (mocked SDK).

Default OFF, fail-open, scoped to the non-Gemini-3 tools path, busts on tool-set change,
deleted on cleanup. No live SDK call.
"""
import logging
import types

import pytest

from modules.llm.gemini_client import GeminiClient


class _FakeCache:
    def __init__(self, sig="s"):
        self.sig = sig
        self.deleted = False

    def delete(self):
        self.deleted = True


def _client(model_type="gemini-2.5-flash"):
    c = object.__new__(GeminiClient)
    c.logger = logging.getLogger("gemini-cache-test")
    c.model_type = model_type
    c._cached_content = None
    c._cached_tool_sig = None
    return c


@pytest.fixture
def fake_create(monkeypatch):
    calls = {"n": 0}

    def _create(**kwargs):
        calls["n"] += 1
        return _FakeCache(sig=str(kwargs.get("tools")))

    monkeypatch.setattr("google.generativeai.caching.CachedContent.create", _create)
    return calls


def _tools(*names):
    return [types.SimpleNamespace(function_declarations=[types.SimpleNamespace(name=n) for n in names])]


def test_disabled_by_default(monkeypatch, fake_create):
    monkeypatch.delenv("GEMINI_PROMPT_CACHE", raising=False)
    c = _client()
    assert c._maybe_build_cached_content("sys" * 5000, _tools("a")) is None
    assert fake_create["n"] == 0


def test_below_min_tokens_returns_none(monkeypatch, fake_create):
    monkeypatch.setenv("GEMINI_PROMPT_CACHE", "true")
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)
    c = _client()
    assert c._maybe_build_cached_content("tiny", _tools("a")) is None
    assert fake_create["n"] == 0


def test_creates_and_reuses(monkeypatch, fake_create):
    monkeypatch.setenv("GEMINI_PROMPT_CACHE", "true")
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)
    c = _client()
    big = "x" * 20000  # > 2048 tokens (~/4)
    first = c._maybe_build_cached_content(big, _tools("a", "b"))
    assert first is not None and fake_create["n"] == 1
    second = c._maybe_build_cached_content(big, _tools("a", "b"))
    assert second is first and fake_create["n"] == 1  # reused, not recreated


def test_busts_on_tool_change(monkeypatch, fake_create):
    monkeypatch.setenv("GEMINI_PROMPT_CACHE", "true")
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)
    c = _client()
    big = "x" * 20000
    first = c._maybe_build_cached_content(big, _tools("a"))
    second = c._maybe_build_cached_content(big, _tools("a", "c"))  # tool set changed
    assert second is not first and fake_create["n"] == 2
    assert first.deleted is True  # old cache cleaned up


def test_gemini3_scoped_out(monkeypatch, fake_create):
    monkeypatch.setenv("GEMINI_PROMPT_CACHE", "true")
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)
    c = _client(model_type="gemini-3-pro-preview")
    assert c._maybe_build_cached_content("x" * 20000, _tools("a")) is None
    assert fake_create["n"] == 0


def test_failopen_on_create_error(monkeypatch):
    monkeypatch.setenv("GEMINI_PROMPT_CACHE", "true")
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)

    def _boom(**kwargs):
        raise RuntimeError("SDK down")

    monkeypatch.setattr("google.generativeai.caching.CachedContent.create", _boom)
    c = _client()
    assert c._maybe_build_cached_content("x" * 20000, _tools("a")) is None


@pytest.mark.asyncio
async def test_cleanup_deletes_cache(monkeypatch):
    c = _client()
    cache = _FakeCache()
    c._cached_content = cache
    c._delete_cached_content()
    assert cache.deleted is True
    assert c._cached_content is None
