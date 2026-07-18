"""D8 — length cap on cross-session memory rows.

Episodes cap task/summary and the curated store caps per-entry chars, but the
auto-injected `memories` rows had NO cap: one oversized tool dump became a
permanent recall-bloat row. The cap lives in `_compose_stored_content` — the
single composition point shared by the keyword and vector halves — so both
stores keep byte-identical strings (RRF dedup-by-content stays consistent).
"""
from __future__ import annotations

import pytest

from modules.memory.sqlite_memory_provider import SqliteMemoryProvider


@pytest.fixture()
def provider(tmp_path):
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


def test_default_cap_truncates(monkeypatch, provider):
    monkeypatch.delenv("MEMORY_ROW_MAX_CHARS", raising=False)
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "true")
    content = provider._compose_stored_content("q", "x" * 10_000)
    assert len(content) == 4000


def test_env_cap_wins(monkeypatch, provider):
    monkeypatch.setenv("MEMORY_ROW_MAX_CHARS", "100")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "true")
    content = provider._compose_stored_content("q", "y" * 500)
    assert len(content) == 100


def test_cap_zero_disables(monkeypatch, provider):
    monkeypatch.setenv("MEMORY_ROW_MAX_CHARS", "0")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "true")
    content = provider._compose_stored_content("q", "z" * 10_000)
    assert len(content) == 10_000


def test_under_cap_unchanged(monkeypatch, provider):
    monkeypatch.delenv("MEMORY_ROW_MAX_CHARS", raising=False)
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "false")
    content = provider._compose_stored_content("question", "answer")
    assert content == "User: question\nAssistant: answer"


@pytest.mark.asyncio
async def test_sync_turn_stores_capped_row(monkeypatch, provider, tmp_path):
    monkeypatch.setenv("MEMORY_ROW_MAX_CHARS", "50")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "true")
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "false")
    await provider.sync_turn("q", "a" * 400, session_id="s1", user_id="user_x")
    rows = provider._keyword_contents("", norm_user="user_x", limit=5)
    assert rows and all(len(r) <= 50 for r in rows)
