"""C3 — `polyrob knowledge export`: compose an Obsidian-compatible markdown vault.

The composer (core/knowledge_export.py) is a projection over the existing
readers — notes (C1 verbs), episodes (paginated recall), skills, identity,
goals. Every section is fail-open: a missing reader yields an empty section,
never a crash. The DBs stay the SSOT; the vault is export-only.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from core.knowledge_export import build_vault, sanitize_filename
from modules.memory.provider import EpisodeRecord
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

USER = "owner-1"


def test_sanitize_filename():
    assert sanitize_filename("prod deploys") == "prod-deploys"
    assert sanitize_filename("a/b\\c: d?") == "a-b-c-d"
    assert sanitize_filename("") == "untitled"


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_REQUIRE_USER_ID", "true")
    monkeypatch.setenv("EPISODIC_MEMORY_ENABLED", "true")
    return SqliteMemoryProvider(str(tmp_path / "memory.db"))


def _seed(provider):
    async def go():
        await provider.note_create(
            USER, "Deploy via [[deploy runbook]] only.", title="prod deploys",
            tags=["ops"], source="session:s1")
        await provider.note_create(
            USER, "Steps for a safe deploy.", title="deploy runbook", tags=["ops"])
        await provider.record_episode(
            EpisodeRecord(ts=int(time.time()), user_id=USER, session_id="s1",
                          kind="goal", task="draft tweet", outcome="done",
                          summary="posted it", artifacts=[{"path": "tweet.txt"}],
                          spend_usd=0.1, steps=3),
            session_id="s1", user_id=USER)
    asyncio.run(go())


def test_build_vault_writes_notes_episodes_index(tmp_path, provider):
    _seed(provider)
    out = tmp_path / "vault"
    manifest = asyncio.run(build_vault(
        str(out), user_id=USER, data_dir=str(tmp_path), provider=provider))
    # notes with YAML frontmatter + intact wikilinks
    note_files = list((out / "notes").glob("*.md"))
    assert len(note_files) == 2
    deploy_note = next(p for p in note_files if "prod-deploys" in p.name)
    text = deploy_note.read_text()
    assert text.startswith("---\n")
    assert "title: " in text and "tags:" in text
    assert "[[deploy runbook]]" in text
    # daily episode note
    day = time.strftime("%Y-%m-%d")
    ep = out / "episodes" / f"{day}.md"
    assert ep.is_file()
    ep_text = ep.read_text()
    assert "draft tweet" in ep_text and "done" in ep_text and "tweet.txt" in ep_text
    # index front page
    idx = (out / "index.md").read_text()
    assert "[[notes/" in idx or "notes" in idx
    assert manifest["notes"] == 2
    assert manifest["episodes"] == 1


def test_notes_carry_title_alias_and_pending_separated(tmp_path, provider):
    """Obsidian resolves [[wikilinks]] by filename or aliases — the title must
    ride as an alias; pending (unreviewed) notes go to notes/pending/, never
    mixed with vetted active notes."""
    _seed(provider)

    async def pending_note():
        await provider.note_create(USER, "unreviewed overnight fact",
                                   title="night learning", status="pending",
                                   created_by="background_review")
    asyncio.run(pending_note())
    out = tmp_path / "vault"
    asyncio.run(build_vault(str(out), user_id=USER, data_dir=str(tmp_path),
                            provider=provider))
    runbook = next(p for p in (out / "notes").glob("*.md")
                   if "deploy-runbook" in p.name)
    assert 'aliases: ["deploy runbook"]' in runbook.read_text()
    pending_files = list((out / "notes" / "pending").glob("*.md"))
    assert len(pending_files) == 1
    assert "unreviewed overnight fact" in pending_files[0].read_text()
    # active dir holds only the two vetted notes
    assert len(list((out / "notes").glob("*.md"))) == 2


def test_build_vault_failopen_without_provider(tmp_path):
    out = tmp_path / "vault"
    manifest = asyncio.run(build_vault(
        str(out), user_id=USER, data_dir=str(tmp_path), provider=None))
    assert (out / "index.md").is_file()
    assert manifest["notes"] == 0 and manifest["episodes"] == 0


def test_export_respects_since(tmp_path, provider):
    _seed(provider)

    async def old_episode():
        await provider.record_episode(
            EpisodeRecord(ts=int(time.time()) - 40 * 86400, user_id=USER,
                          session_id="s-old", kind="cron", task="old tick",
                          outcome="done"),
            session_id="s-old", user_id=USER)
    asyncio.run(old_episode())
    out = tmp_path / "vault"
    manifest = asyncio.run(build_vault(
        str(out), user_id=USER, data_dir=str(tmp_path), provider=provider,
        since_ts=int(time.time()) - 7 * 86400))
    assert manifest["episodes"] == 1  # the old one is outside the window
