"""Memory store calls run off the event loop (proposal 125 S18a, B-H6).

The curated-notes, episode and knowledge-base store methods were ``async def``
bodies that called SQLite inline: a contended ``memory.db`` held every session.
Each one is ``@offloaded`` now (or awaits a store call that is).
"""
import ast
import inspect
import threading
from pathlib import Path

import pytest

import modules.memory.sqlite_curated_store as curated_mod
import modules.memory.sqlite_episodes_store as episodes_mod
import modules.memory.sqlite_kb_store as kb_mod
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

USER = "owner-1"


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


def _record_threads(monkeypatch, module):
    seen = []
    real = module.execute_retry

    def _spy(*a, **k):
        seen.append(threading.get_ident())
        return real(*a, **k)
    monkeypatch.setattr(module, "execute_retry", _spy)
    return seen


@pytest.mark.asyncio
async def test_note_list_runs_off_the_loop_thread(provider, monkeypatch):
    await provider.note_create(USER, "a fact", title="fact")
    seen = _record_threads(monkeypatch, curated_mod)
    await provider.note_list(USER)
    assert seen and threading.get_ident() not in seen


@pytest.mark.asyncio
async def test_recall_episodes_runs_off_the_loop_thread(provider, monkeypatch):
    seen = _record_threads(monkeypatch, episodes_mod)
    await provider.recall_episodes(user_id=USER, limit=5)
    assert seen and threading.get_ident() not in seen


@pytest.mark.asyncio
async def test_kb_list_sources_runs_off_the_loop_thread(provider, monkeypatch):
    seen = _record_threads(monkeypatch, kb_mod)
    await provider.kb_list_sources(user_id=USER)
    assert seen and threading.get_ident() not in seen


@pytest.mark.parametrize("module", [curated_mod, episodes_mod, kb_mod])
def test_every_async_store_method_is_offloaded_or_awaits(module):
    """An ``async def`` in these stores either carries ``@offloaded`` or awaits."""
    tree = ast.parse(Path(inspect.getfile(module)).read_text())
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        offloaded = any(getattr(d, "id", None) == "offloaded" for d in node.decorator_list)
        awaits = any(isinstance(n, ast.Await) for n in ast.walk(node))
        if not (offloaded or awaits):
            bad.append(node.name)
    assert bad == [], f"blocking store call on the event loop: {bad}"
