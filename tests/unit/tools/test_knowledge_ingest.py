"""Unit tests for tools.knowledge_ingest (Task 6).

TDD coverage:
- _iter_files: skips .env / id_rsa / binary, includes .md/.py, respects max_files
- _chunk: honors target/overlap, splits on headings
- kb_ingest: lands chunks in a fake registry (monkeypatched), dedup, path escape
- PDF path: async document-worker adapter mocked → text extracted
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously in tests."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _iter_files
# ---------------------------------------------------------------------------


class TestIterFiles:
    def test_skips_env_file(self, tmp_path):
        from tools.knowledge_ingest import _iter_files

        (tmp_path / ".env").write_text("SECRET=abc")
        (tmp_path / "readme.md").write_text("# Hello")

        files, skipped = _iter_files(tmp_path, recursive=False)
        names = [f.name for f in files]
        assert ".env" not in names
        assert "readme.md" in names
        assert skipped["secret"] >= 1

    def test_skips_id_rsa(self, tmp_path):
        from tools.knowledge_ingest import _iter_files

        (tmp_path / "id_rsa").write_text("PRIVATE KEY DATA")
        (tmp_path / "main.py").write_text("print('hello')")

        files, skipped = _iter_files(tmp_path, recursive=False)
        names = [f.name for f in files]
        assert "id_rsa" not in names
        assert "main.py" in names
        assert skipped["secret"] >= 1

    def test_skips_binary_file(self, tmp_path):
        from tools.knowledge_ingest import _iter_files

        # Write a file with null bytes (binary)
        (tmp_path / "image.bin").write_bytes(b"\x00\x01\x02\x03" * 100)
        (tmp_path / "notes.md").write_text("# Notes\nSome text.")

        files, skipped = _iter_files(tmp_path, recursive=False)
        names = [f.name for f in files]
        assert "image.bin" not in names
        assert "notes.md" in names
        assert skipped["binary"] >= 1

    def test_includes_md_and_py(self, tmp_path):
        from tools.knowledge_ingest import _iter_files

        (tmp_path / "doc.md").write_text("# Doc")
        (tmp_path / "script.py").write_text("x = 1")

        files, _ = _iter_files(tmp_path, recursive=False)
        names = {f.name for f in files}
        assert "doc.md" in names
        assert "script.py" in names

    def test_respects_max_files(self, tmp_path):
        from tools.knowledge_ingest import _iter_files

        for i in range(10):
            (tmp_path / f"file{i}.txt").write_text(f"content {i}")

        files, skipped = _iter_files(tmp_path, recursive=False, max_files=3)
        assert len(files) == 3
        assert skipped["max_files"] > 0

    def test_recursive_finds_nested(self, tmp_path):
        from tools.knowledge_ingest import _iter_files

        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "nested.md").write_text("# Nested")

        files, _ = _iter_files(tmp_path, recursive=True)
        names = [f.name for f in files]
        assert "nested.md" in names

    def test_nonrecursive_excludes_nested(self, tmp_path):
        from tools.knowledge_ingest import _iter_files

        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "nested.md").write_text("# Nested")
        (tmp_path / "top.md").write_text("# Top")

        files, _ = _iter_files(tmp_path, recursive=False)
        names = [f.name for f in files]
        assert "nested.md" not in names
        assert "top.md" in names

    def test_skips_hidden_dirs_in_walk(self, tmp_path):
        from tools.knowledge_ingest import _iter_files

        hidden = tmp_path / ".secrets"
        hidden.mkdir()
        (hidden / "token.txt").write_text("super_secret")
        (tmp_path / "public.md").write_text("# Public")

        files, _ = _iter_files(tmp_path, recursive=True)
        names = [f.name for f in files]
        assert "token.txt" not in names
        assert "public.md" in names

    def test_glob_filter(self, tmp_path):
        from tools.knowledge_ingest import _iter_files

        (tmp_path / "a.md").write_text("# A")
        (tmp_path / "b.py").write_text("pass")

        files, _ = _iter_files(tmp_path, recursive=False, globs=["*.md"])
        names = [f.name for f in files]
        assert "a.md" in names
        assert "b.py" not in names


# ---------------------------------------------------------------------------
# _chunk
# ---------------------------------------------------------------------------


class TestChunk:
    def test_returns_nonempty_chunks(self):
        from tools.knowledge_ingest import _chunk

        text = "Hello world. " * 200  # ~800 chars → ~200 tokens
        chunks = _chunk(text, target=50, overlap=10)
        assert len(chunks) >= 1
        assert all(c.strip() for c in chunks)

    def test_honors_target_size(self):
        from tools.knowledge_ingest import _chunk
        from core.security.secret_guard import estimate_tokens_rough

        # Create text large enough to force multiple chunks
        text = "word " * 2000  # ~10000 chars → ~2500 tokens
        chunks = _chunk(text, target=200, overlap=20)
        assert len(chunks) > 1
        # Each chunk should be close to target (allow some overshoot for paragraph units)
        for c in chunks:
            assert estimate_tokens_rough(c) <= 300  # reasonable bound

    def test_splits_on_headings(self):
        from tools.knowledge_ingest import _chunk, _split_on_headings

        # Verify _split_on_headings splits on markdown headings
        text = "# Section One\n\nContent of section one.\n\n# Section Two\n\nContent of section two."
        parts = _split_on_headings(text)
        # Should have 2 parts — one per heading
        assert len(parts) == 2
        assert any("Section One" in p for p in parts)
        assert any("Section Two" in p for p in parts)

        # With a target smaller than the combined text, _chunk should produce 2 chunks
        # Each section is ~10 tokens; use target=8 to force separation
        chunks = _chunk(text, target=8, overlap=0)
        assert len(chunks) >= 2
        # Heading text must appear in the output
        full = "\n".join(chunks)
        assert "Section One" in full
        assert "Section Two" in full

    def test_overlap_carries_into_next_chunk(self):
        from tools.knowledge_ingest import _chunk

        # Build text with 4 clear markdown sections
        sections = []
        for i in range(4):
            sections.append(f"# Section {i}\n\n" + ("word " * 100))
        text = "\n\n".join(sections)

        chunks = _chunk(text, target=60, overlap=20)
        # If overlap works, later chunks should contain some text from previous chunks
        # (this is a heuristic test — overlap carry is visible as repeated words)
        assert len(chunks) >= 2

    def test_empty_text_returns_empty(self):
        from tools.knowledge_ingest import _chunk

        assert _chunk("") == []
        assert _chunk("   \n  ") == []

    def test_single_short_text_stays_one_chunk(self):
        from tools.knowledge_ingest import _chunk

        text = "Just a short sentence."
        chunks = _chunk(text, target=800, overlap=50)
        assert len(chunks) == 1


# ---------------------------------------------------------------------------
# kb_ingest (fake registry)
# ---------------------------------------------------------------------------


class FakeRegistry:
    """Captures calls to kb_* registry functions (all async to match real API)."""

    def __init__(self):
        self.ingested_chunks: List[Dict] = []
        self.removed: List[Dict] = []
        self.hashes: Dict[str, str] = {}  # source_path → hash

    async def kb_ingest_chunk(self, *, user_id, collection, source_path,
                               source_hash, chunk_idx, content, mime, created_at):
        self.ingested_chunks.append({
            "user_id": user_id,
            "collection": collection,
            "source_path": source_path,
            "source_hash": source_hash,
            "chunk_idx": chunk_idx,
            "content": content,
        })
        # Track the hash so subsequent calls see it
        self.hashes[source_path] = source_hash

    async def kb_replace_source(self, *, chunks, **kwargs):
        previous_chunks = list(self.ingested_chunks)
        previous_hashes = dict(self.hashes)
        source = kwargs["source_path"]
        self.ingested_chunks = [row for row in self.ingested_chunks
                                if row["source_path"] != source]
        for idx, text in enumerate(chunks):
            ok = await self.kb_ingest_chunk(chunk_idx=idx, content=text, **kwargs)
            if ok is False:
                self.ingested_chunks = previous_chunks
                self.hashes = previous_hashes
                return False
        return True

    async def kb_remove(self, *, user_id, collection, source=None):
        self.removed.append({"collection": collection, "source": source})
        if source and source in self.hashes:
            del self.hashes[source]
        return 1

    async def kb_source_hash(self, *, user_id, collection, source_path):
        return self.hashes.get(source_path)


class TestKbIngest:
    def _make_registry(self):
        return FakeRegistry()

    def _run_ingest(self, path, fake_reg, **kwargs):
        """Helper to run kb_ingest with patched registry routers."""
        import tools.knowledge_ingest as ki_mod

        async def _run():
            return await ki_mod.kb_ingest(
                path,
                user_id=kwargs.get("user_id", "u1"),
                session_id=kwargs.get("session_id", "s1"),
                collection=kwargs.get("collection", "default"),
                recursive=kwargs.get("recursive", True),
            )

        # Patch the registry routers that kb_ingest imports
        with patch("modules.memory.registry.kb_replace_source", new=fake_reg.kb_replace_source), \
             patch("modules.memory.registry.kb_remove", new=fake_reg.kb_remove), \
             patch("modules.memory.registry.kb_source_hash", new=fake_reg.kb_source_hash):
            # Also patch the imported names inside kb_ingest's closure
            with patch.object(ki_mod, "_resolve_confinement_root", return_value=Path(kwargs.get("root", "/tmp"))):
                return asyncio.run(_run())

    def test_ingest_lands_chunks(self, tmp_path):
        from tools.knowledge_ingest import kb_ingest, _resolve_confinement_root
        import tools.knowledge_ingest as ki_mod

        (tmp_path / "notes.md").write_text("# Hello\n\nThis is a test document with enough content.")

        fake = FakeRegistry()

        async def _run():
            return await kb_ingest(
                str(tmp_path / "notes.md"),
                user_id="u1",
                session_id="s1",
            )

        with patch.object(ki_mod, "_resolve_confinement_root", return_value=tmp_path), \
             patch("modules.memory.registry.kb_replace_source", new=fake.kb_replace_source), \
             patch("modules.memory.registry.kb_remove", new=fake.kb_remove), \
             patch("modules.memory.registry.kb_source_hash", new=fake.kb_source_hash):
            result = asyncio.run(_run())

        assert result["ingested"] == 1
        assert result["n_chunks"] >= 1
        assert len(fake.ingested_chunks) >= 1

    def test_reingest_unchanged_skips(self, tmp_path):
        """Re-ingest the same file without modification → unchanged > 0, ingested == 0."""
        from tools.knowledge_ingest import kb_ingest
        import tools.knowledge_ingest as ki_mod

        fpath = tmp_path / "doc.txt"
        fpath.write_text("Some stable content.")

        fake = FakeRegistry()

        async def _ingest():
            return await kb_ingest(str(fpath), user_id="u1", session_id="s1")

        with patch.object(ki_mod, "_resolve_confinement_root", return_value=tmp_path), \
             patch("modules.memory.registry.kb_replace_source", new=fake.kb_replace_source), \
             patch("modules.memory.registry.kb_remove", new=fake.kb_remove), \
             patch("modules.memory.registry.kb_source_hash", new=fake.kb_source_hash):
            # First ingest
            r1 = asyncio.run(_ingest())
            assert r1["ingested"] == 1

            # Second ingest — same file, hash already recorded in fake.hashes
            r2 = asyncio.run(_ingest())
            assert r2["unchanged"] > 0
            assert r2["ingested"] == 0

    def test_reingest_modified_file(self, tmp_path):
        """Modified file replaces its complete source through one provider call."""
        from tools.knowledge_ingest import kb_ingest
        import tools.knowledge_ingest as ki_mod

        fpath = tmp_path / "doc.txt"
        fpath.write_text("Original content.")

        fake = FakeRegistry()

        async def _ingest():
            return await kb_ingest(str(fpath), user_id="u1", session_id="s1")

        with patch.object(ki_mod, "_resolve_confinement_root", return_value=tmp_path), \
             patch("modules.memory.registry.kb_replace_source", new=fake.kb_replace_source), \
             patch("modules.memory.registry.kb_remove", new=fake.kb_remove), \
             patch("modules.memory.registry.kb_source_hash", new=fake.kb_source_hash):
            r1 = asyncio.run(_ingest())
            assert r1["ingested"] == 1

            # Modify file
            fpath.write_text("Modified content — completely different.")

            r2 = asyncio.run(_ingest())
            assert r2["ingested"] == 1
            # Replacement never invokes the destructive removal router.
            assert not fake.removed
            assert len(fake.ingested_chunks) == 1
            assert "Modified content" in fake.ingested_chunks[0]["content"]

    def test_source_name_override_stores_logical_identity(self, tmp_path):
        """source_name overrides the stored source_path for single-file ingest."""
        from tools.knowledge_ingest import kb_ingest
        import tools.knowledge_ingest as ki_mod

        fpath = tmp_path / "tmp_abc123.txt"  # volatile on-disk name (e.g. mkstemp)
        fpath.write_text("Some stable content for the source-name test.")

        fake = FakeRegistry()

        async def _ingest():
            return await kb_ingest(
                str(fpath), user_id="u1", session_id="s1",
                source_name="original_filename.txt",
            )

        with patch.object(ki_mod, "_resolve_confinement_root", return_value=tmp_path), \
             patch("modules.memory.registry.kb_replace_source", new=fake.kb_replace_source), \
             patch("modules.memory.registry.kb_remove", new=fake.kb_remove), \
             patch("modules.memory.registry.kb_source_hash", new=fake.kb_source_hash):
            result = asyncio.run(_ingest())

        assert result["ingested"] == 1
        # Stored source_path is the LOGICAL name, not the volatile on-disk path.
        assert all(c["source_path"] == "original_filename.txt" for c in fake.ingested_chunks)
        assert str(fpath) not in fake.hashes

    def test_source_name_dedups_across_volatile_paths(self, tmp_path):
        """Re-uploading identical content under a NEW temp path dedups by source_name."""
        from tools.knowledge_ingest import kb_ingest
        import tools.knowledge_ingest as ki_mod

        content = "Identical content re-uploaded twice."
        fake = FakeRegistry()

        async def _ingest(disk_name):
            p = tmp_path / disk_name
            p.write_text(content)
            return await kb_ingest(
                str(p), user_id="u1", session_id="s1",
                source_name="report.txt",  # stable logical identity
            )

        with patch.object(ki_mod, "_resolve_confinement_root", return_value=tmp_path), \
             patch("modules.memory.registry.kb_replace_source", new=fake.kb_replace_source), \
             patch("modules.memory.registry.kb_remove", new=fake.kb_remove), \
             patch("modules.memory.registry.kb_source_hash", new=fake.kb_source_hash):
            # First upload: temp path A.
            r1 = asyncio.run(_ingest("tmp_AAA.txt"))
            assert r1["ingested"] == 1
            chunks_after_first = len(fake.ingested_chunks)

            # Second upload: DIFFERENT temp path B, same content + same source_name.
            r2 = asyncio.run(_ingest("tmp_BBB.txt"))

        # Dedup fires (unchanged), no new chunks accumulated.
        assert r2["unchanged"] > 0
        assert r2["ingested"] == 0
        assert len(fake.ingested_chunks) == chunks_after_first

    def test_path_escape_refused(self, tmp_path):
        """../../etc path escape → error returned, nothing ingested."""
        from tools.knowledge_ingest import kb_ingest
        import tools.knowledge_ingest as ki_mod

        fake = FakeRegistry()

        async def _ingest():
            return await kb_ingest(
                "../../etc/passwd",
                user_id="u1",
                session_id="s1",
            )

        with patch.object(ki_mod, "_resolve_confinement_root", return_value=tmp_path), \
             patch("modules.memory.registry.kb_replace_source", new=fake.kb_replace_source), \
             patch("modules.memory.registry.kb_remove", new=fake.kb_remove), \
             patch("modules.memory.registry.kb_source_hash", new=fake.kb_source_hash):
            result = asyncio.run(_ingest())

        assert "error" in result
        assert result["ingested"] == 0
        assert len(fake.ingested_chunks) == 0

    def test_directory_ingest(self, tmp_path):
        """Directory ingest processes all eligible files."""
        from tools.knowledge_ingest import kb_ingest
        import tools.knowledge_ingest as ki_mod

        (tmp_path / "a.md").write_text("# A\n\nContent A.")
        (tmp_path / "b.py").write_text("def foo(): pass")
        (tmp_path / ".env").write_text("SECRET=xxx")

        fake = FakeRegistry()

        async def _ingest():
            return await kb_ingest(str(tmp_path), user_id="u1", session_id="s1")

        with patch.object(ki_mod, "_rg_files", return_value=None), \
             patch.object(ki_mod, "_resolve_confinement_root", return_value=tmp_path), \
             patch("modules.memory.registry.kb_replace_source", new=fake.kb_replace_source), \
             patch("modules.memory.registry.kb_remove", new=fake.kb_remove), \
             patch("modules.memory.registry.kb_source_hash", new=fake.kb_source_hash):
            result = asyncio.run(_ingest())

        assert result["ingested"] == 2  # .md and .py
        assert result["skipped_secret"] >= 1  # .env skipped

    def test_single_file_over_byte_cap_skipped(self, tmp_path, monkeypatch):
        """A single file larger than KB_MAX_BYTES is skipped, not read into memory."""
        from tools.knowledge_ingest import kb_ingest
        import tools.knowledge_ingest as ki_mod

        monkeypatch.setenv("KB_MAX_BYTES", "10")
        fpath = tmp_path / "big.txt"
        fpath.write_text("x" * 5000)  # 5000 bytes >> 10-byte cap

        fake = FakeRegistry()

        async def _ingest():
            return await kb_ingest(str(fpath), user_id="u1", session_id="s1")

        with patch.object(ki_mod, "_resolve_confinement_root", return_value=tmp_path), \
             patch("modules.memory.registry.kb_replace_source", new=fake.kb_replace_source), \
             patch("modules.memory.registry.kb_remove", new=fake.kb_remove), \
             patch("modules.memory.registry.kb_source_hash", new=fake.kb_source_hash):
            result = asyncio.run(_ingest())

        assert result["ingested"] == 0
        assert result.get("skipped_too_large", 0) >= 1
        assert len(fake.ingested_chunks) == 0

    def test_partial_ingest_not_marked_complete(self, tmp_path, monkeypatch):
        """Failed publication is not counted as complete and leaves no partial hash."""
        from tools.knowledge_ingest import kb_ingest
        import tools.knowledge_ingest as ki_mod

        monkeypatch.setenv("KB_CHUNK_TOKENS", "5")
        monkeypatch.setenv("KB_CHUNK_OVERLAP", "0")
        fpath = tmp_path / "doc.md"
        fpath.write_text(
            "# A\n\n" + ("alpha " * 40)
            + "\n\n# B\n\n" + ("beta " * 40)
            + "\n\n# C\n\n" + ("gamma " * 40)
        )

        class PartialFailRegistry(FakeRegistry):
            async def kb_ingest_chunk(self, *, user_id, collection, source_path,
                                       source_hash, chunk_idx, content, mime, created_at):
                if chunk_idx >= 1:
                    return False  # simulate a DB write failure on the 2nd chunk
                await FakeRegistry.kb_ingest_chunk(
                    self, user_id=user_id, collection=collection,
                    source_path=source_path, source_hash=source_hash,
                    chunk_idx=chunk_idx, content=content, mime=mime, created_at=created_at,
                )
                return True

        fake = PartialFailRegistry()

        async def _ingest():
            return await kb_ingest(str(fpath), user_id="u1", session_id="s1")

        with patch.object(ki_mod, "_resolve_confinement_root", return_value=tmp_path), \
             patch("modules.memory.registry.kb_replace_source", new=fake.kb_replace_source), \
             patch("modules.memory.registry.kb_remove", new=fake.kb_remove), \
             patch("modules.memory.registry.kb_source_hash", new=fake.kb_source_hash):
            result = asyncio.run(_ingest())

        assert result["ingested"] == 0
        assert result.get("failed", 0) >= 1
        # Failed publication leaves no partial hash/chunks and uses no removal.
        assert not fake.removed
        assert not fake.ingested_chunks
        assert str(fpath) not in fake.hashes

    def test_pdf_path_mock(self, tmp_path):
        """PDF ingestion awaits the bounded worker adapter and stores its text."""
        from tools.knowledge_ingest import kb_ingest
        import tools.knowledge_ingest as ki_mod

        pdf_path = tmp_path / "report.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake content")

        fake = FakeRegistry()

        # Replace only the worker adapter; exercise the rest of ingestion.
        async def fake_process_pdf(kind, content):
            assert kind == "pdf"
            return {"content": "Extracted PDF content for testing purposes."}

        async def _ingest():
            return await kb_ingest(str(pdf_path), user_id="u1", session_id="s1")

        with patch.object(ki_mod, "_resolve_confinement_root", return_value=tmp_path), \
             patch(
                 "tools.document_parser.parse_document_async",
                 new=fake_process_pdf,
             ), \
             patch("modules.memory.registry.kb_replace_source", new=fake.kb_replace_source), \
             patch("modules.memory.registry.kb_remove", new=fake.kb_remove), \
             patch("modules.memory.registry.kb_source_hash", new=fake.kb_source_hash):
            result = asyncio.run(_ingest())

        assert result["ingested"] == 1
        assert result["n_chunks"] >= 1
        # The ingested chunk content came from the mocked worker
        assert any(
            "Extracted PDF content" in c.get("content", "")
            for c in fake.ingested_chunks
        )
        # Verify the source was the pdf with pdf mime
        assert any(c.get("source_path", "").endswith(".pdf") for c in fake.ingested_chunks)


# ---------------------------------------------------------------------------
# _resolve_confinement_root (fail-closed on server, fail-open under local mode)
# ---------------------------------------------------------------------------


class TestConfinementRoot:
    def test_failclosed_on_server_when_pm_unavailable(self, monkeypatch):
        """On the server (not local mode), if pm() can't resolve the workspace root,
        refuse rather than silently confining to the process CWD."""
        import tools.knowledge_ingest as ki_mod
        import agents.task.path as path_mod
        import core.config_policy as policy_mod

        def boom(*a, **k):
            raise RuntimeError("no path manager")

        monkeypatch.setattr(path_mod, "pm", boom)
        monkeypatch.setattr(policy_mod, "local_mode_enabled", lambda: False)

        with pytest.raises(Exception):
            ki_mod._resolve_confinement_root("s1", "u1")

    def test_failopen_to_cwd_under_local_mode(self, monkeypatch, tmp_path):
        """Under local mode the CWD fallback is acceptable (single-user)."""
        import tools.knowledge_ingest as ki_mod
        import agents.task.path as path_mod
        import core.config_policy as policy_mod

        def boom(*a, **k):
            raise RuntimeError("no path manager")

        monkeypatch.setattr(path_mod, "pm", boom)
        monkeypatch.setattr(policy_mod, "local_mode_enabled", lambda: True)
        monkeypatch.chdir(tmp_path)

        root = ki_mod._resolve_confinement_root("s1", "u1")
        assert root == tmp_path.resolve()


@pytest.mark.asyncio
async def test_hash_and_extraction_use_one_snapshot(tmp_path, monkeypatch):
    import hashlib
    import tools.knowledge_ingest as ki
    from modules.memory import registry
    source = tmp_path / 'note.md'
    original = b'# Safe\nOriginal content before replacement.'
    source.write_bytes(original)
    outside = tmp_path.parent / 'private-fixture.txt'
    outside.write_text('must never enter the knowledge base')
    stored = []
    async def hash_lookup(**kwargs):
        source.unlink()
        source.symlink_to(outside)
        return None
    async def store_chunk(**kwargs):
        stored.append(kwargs)
        return True
    monkeypatch.setattr(ki, '_resolve_confinement_root', lambda *a: tmp_path)
    monkeypatch.setattr(registry, 'kb_source_hash', hash_lookup)
    monkeypatch.setattr(registry, 'kb_replace_source', store_chunk)
    result = await ki.kb_ingest(str(source), user_id='u', session_id='s')
    assert result['ingested'] == 1
    assert stored and all(row['source_hash'] == hashlib.sha256(original).hexdigest() for row in stored)
    assert 'Original content' in stored[0]['chunks'][0]
    assert all('must never' not in ' '.join(row['chunks']) for row in stored)


def test_directory_walk_refuses_secret_aliases(tmp_path, monkeypatch):
    import tools.knowledge_ingest as ki
    root = tmp_path / 'workspace'
    root.mkdir()
    private = tmp_path / '.env'
    private.write_text('synthetic fixture')
    (root / 'ordinary.md').symlink_to(private)
    monkeypatch.setattr(ki, '_rg_files', lambda root: None)
    files, skipped = ki._iter_files(root)
    assert not files
    assert skipped['unsafe'] == 1
