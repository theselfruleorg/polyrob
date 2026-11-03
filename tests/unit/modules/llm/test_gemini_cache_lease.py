"""098 MO-14: a Gemini cachedContents handle is a lease (TTL, key, drop on error)."""
import logging
import types

import pytest

from functools import partial

from modules.llm import gemini_cache
from modules.llm.gemini_cache import CacheLeases, MAX_LEASES, cache_key, leases_of

build_or_reuse = partial(gemini_cache.build_or_reuse, min_tokens=2048)


class _Cache:
    def __init__(self, n):
        self.n, self.deleted = n, False

    def delete(self):
        self.deleted = True


class _Client:
    def __init__(self, model="gemini-2.5-flash"):
        self.model_type = model
        self.logger = logging.getLogger("t")
        self._cache_leases = None

    def _is_gemini_3_model(self):
        return False


def _tools(*names):
    return [types.SimpleNamespace(function_declarations=[types.SimpleNamespace(name=n) for n in names])]


@pytest.fixture
def created(monkeypatch):
    made = []
    fake = types.SimpleNamespace(caching=types.SimpleNamespace(CachedContent=types.SimpleNamespace(
        create=lambda **kw: made.append(_Cache(len(made))) or made[-1])))
    import sys
    monkeypatch.setitem(sys.modules, "google.generativeai", fake)
    google = sys.modules.get("google") or types.ModuleType("google")
    monkeypatch.setattr(google, "generativeai", fake, raising=False)
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setenv("GEMINI_PROMPT_CACHE", "true")
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)
    return made


BIG = "x" * 20000


def test_expired_handle_is_rebuilt(created):
    now = [0.0]
    c = _Client()
    c._cache_leases = CacheLeases(clock=lambda: now[0])
    first = build_or_reuse(c, BIG, _tools("a"))
    assert build_or_reuse(c, BIG, _tools("a")) is first
    now[0] = 600 - 29  # inside the 30 s margin before the 10 min TTL
    second = build_or_reuse(c, BIG, _tools("a"))
    assert second is not first and first.deleted


def test_cache_error_clears_handle_and_retries_uncached(created):
    c = _Client()
    first = build_or_reuse(c, BIG, _tools("a"))
    leases_of(c).drop(first)  # what the client does when a request with it failed
    assert first.deleted and build_or_reuse(c, BIG, _tools("a")) is not first


def test_two_system_prompts_keep_two_handles(created):
    c = _Client()
    a = build_or_reuse(c, BIG + "A", _tools("t"))
    b = build_or_reuse(c, BIG + "B", _tools("t"))
    assert build_or_reuse(c, BIG + "A", _tools("t")) is a and not a.deleted
    assert build_or_reuse(c, BIG + "B", _tools("t")) is b and len(created) == 2


def test_model_change_does_not_reuse_cache(created):
    c = _Client()
    a = build_or_reuse(c, BIG, _tools("t"))
    c.model_type = "gemini-2.5-pro"
    assert build_or_reuse(c, BIG, _tools("t")) is not a
    assert cache_key("m1", "s", []) != cache_key("m2", "s", [])


def test_leases_are_lru_bounded():
    leases = CacheLeases()
    made = [_Cache(i) for i in range(MAX_LEASES + 1)]
    for i, cache in enumerate(made):
        leases.put(("m", str(i), ""), cache, 600)
    assert len(leases) == MAX_LEASES and made[0].deleted and not made[-1].deleted
