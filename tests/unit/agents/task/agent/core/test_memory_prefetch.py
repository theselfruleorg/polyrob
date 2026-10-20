"""B-T2 — memory prefetch into the step path.

`build_prefetch_message` routes a query through the active memory provider and wraps
any recalled text as a MEMORY-origin control message for injection. With the default
Null provider it returns None (prod unchanged); with an external provider it returns
an enveloped HumanMessage. Fail-open: a provider error yields None, never raises.
"""
import pytest

from modules.memory.provider import NullMemoryProvider
from modules.memory.registry import (
    reset_memory_registry,
    set_external_memory_provider,
)
from modules.llm.messages import MessageOrigin
from agents.task.agent.core.memory_prefetch import build_prefetch_message


class _FakeExternalProvider(NullMemoryProvider):
    def __init__(self, recalled):
        self._recalled = recalled

    @property
    def is_external(self) -> bool:
        return True

    async def prefetch(self, query, *, session_id, user_id=None):
        return self._recalled


class _BoomProvider(NullMemoryProvider):
    @property
    def is_external(self) -> bool:
        return True

    async def prefetch(self, query, *, session_id, user_id=None):
        raise RuntimeError("backend down")


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_memory_registry()
    yield
    reset_memory_registry()


@pytest.mark.asyncio
async def test_null_provider_returns_none():
    # Default registry (Null) -> no injection.
    assert await build_prefetch_message("what did we do?", session_id="s1") is None


@pytest.mark.asyncio
async def test_external_recall_returns_memory_message():
    set_external_memory_provider(_FakeExternalProvider("User prefers dark mode."))
    msg = await build_prefetch_message("preferences?", session_id="s1")
    assert msg is not None
    assert getattr(msg, "origin", None) == MessageOrigin.RECALL
    assert "User prefers dark mode." in msg.content


@pytest.mark.asyncio
async def test_empty_recall_returns_none():
    set_external_memory_provider(_FakeExternalProvider("   "))
    assert await build_prefetch_message("preferences?", session_id="s1") is None


@pytest.mark.asyncio
async def test_provider_error_is_fail_open_but_named():
    """D3 / E-A9 (review 2026-10-13): a provider error never breaks the step, but
    it is no longer silent "no memory" — the model gets one RECALL note naming
    the error class and telling it not to claim absence."""
    set_external_memory_provider(_BoomProvider())
    msg = await build_prefetch_message("preferences?", session_id="s1")
    assert msg is not None
    text = msg.content if isinstance(msg.content, str) else str(msg.content)
    assert "Memory recall is unavailable this turn" in text
    assert "never recorded" in text


@pytest.mark.asyncio
async def test_blank_query_returns_none():
    set_external_memory_provider(_FakeExternalProvider("something"))
    assert await build_prefetch_message("", session_id="s1") is None


# ---- 2026-10-13 review: D10 clip, D5 owner notes ---------------------------

def test_d10_long_entry_is_clipped_around_the_matched_part():
    from agents.task.agent.core.memory_prefetch import RECALL_ENTRY_MAX_CHARS, _clip_entry
    filler = "Ledger line about something unrelated to the question. " * 60
    entry = f"- [2026-10-12] {filler}The bridge to Base arrived with 0.5 ETH. {filler}"
    out = _clip_entry(entry, RECALL_ENTRY_MAX_CHARS, ["bridge", "Base", "arrived"])
    assert len(out) <= RECALL_ENTRY_MAX_CHARS + 2
    assert out.startswith("- [2026-10-12] ")
    assert "The bridge to Base arrived with 0.5 ETH." in out
    # without terms it stays a head clip
    assert "bridge" not in _clip_entry(entry, RECALL_ENTRY_MAX_CHARS, None)


class _NotesProvider(_FakeExternalProvider):
    def __init__(self, recalled, notes):
        super().__init__(recalled)
        self._notes = notes

    async def note_list(self, user_id, *, status="active", tag=None, limit=200):
        return list(self._notes)


@pytest.mark.asyncio
async def test_d5_owner_console_notes_reach_the_agent():
    """D5: "I saved that to my notes." — an owner-authored console note
    (created_by='user') is injected with recall; agent notes are not."""
    set_external_memory_provider(_NotesProvider("", [
        {"content": "My accountant is Ana, invoices go to ana@example.org",
         "title": "Accountant", "created_by": "user", "updated_ts": 1_760_000_000},
        {"content": "agent scratch note", "created_by": "agent"},
    ]))
    msg = await build_prefetch_message("send the invoice", session_id="s1", user_id="u",
                                       owner_notes=True)
    assert msg is not None
    assert "Notes the owner saved" in msg.content
    assert "My accountant is Ana" in msg.content
    assert "agent scratch note" not in msg.content
    # not offered when the regime has no owner memory
    assert await build_prefetch_message("send the invoice", session_id="s1", user_id="u",
                                        owner_notes=False) is None


@pytest.mark.asyncio
async def test_d5_owner_notes_injected_once_per_run():
    set_external_memory_provider(_NotesProvider("", [
        {"content": "Owner budget is 200 USD", "created_by": "user"}]))
    seen = set()
    first = await build_prefetch_message("budget", session_id="s1", user_id="u",
                                         seen=seen, owner_notes=True)
    again = await build_prefetch_message("budget", session_id="s1", user_id="u",
                                         seen=seen, owner_notes=True)
    assert first is not None and again is None


@pytest.mark.asyncio
async def test_a8_kb_chunks_carry_their_snapshot_date_and_a_changed_file_is_named(
        tmp_path, monkeypatch):
    """A8 (review 2026-10-13): a 10-02 KB copy of a live ledger was served as current."""
    import os
    import time
    from agents.task.agent.core import memory_prefetch as mp
    live = tmp_path / "ledger.md"
    live.write_text("now")
    os.utime(live, (time.time(), time.time()))

    async def fake_sources(*, user_id=None, collection=None):
        return [{"source_path": str(live), "created_at": "2026-10-02T08:00:00+00:00"},
                {"source_path": "notes.md", "created_at": "2026-10-10T08:00:00+00:00"}]
    monkeypatch.setattr("modules.memory.registry.kb_list_sources", fake_sources)
    text = f"[{live} #37] open book 0.006 WETH\n[notes.md #1] owner note"
    out = await mp._date_kb_snapshots(text, user_id="u", collection="default")
    assert f"[{live} #37, snapshot 2026-10-02; the live file changed since" in out
    assert "[notes.md #1, snapshot 2026-10-10] owner note" in out


def test_d6_clip_inside_an_unverified_line_keeps_the_marker():
    """Re-validation: the D10 match-centred clip cut the leading "[unverified] "
    off a long finding line, so a guess recalled as a fact."""
    from agents.task.agent.core.memory_prefetch import RECALL_ENTRY_MAX_CHARS, _clip_entry
    entry = ("- [2026-10-13] Verified fact.\n[unverified] " + "x " * 900
             + "the ledger file does not exist " + "y " * 900)
    out = _clip_entry(entry, RECALL_ENTRY_MAX_CHARS, ["ledger"])
    assert "ledger file does not exist" in out and "[unverified] " in out
    assert len(out) <= RECALL_ENTRY_MAX_CHARS
