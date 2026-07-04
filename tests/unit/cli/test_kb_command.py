"""Unit tests for `polyrob kb` command group (Task 8).

Uses CliRunner + monkeypatching so no real embedder / sqlite is needed.
All async engine/registry calls are mocked at the module boundary.
"""
from __future__ import annotations

import asyncio

import pytest
from click.testing import CliRunner

from cli.commands.kb import kb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _async_return(value):
    """Return a coroutine that yields *value* — usable as a mock side_effect."""
    async def _coro(*args, **kwargs):
        return value
    return _coro


async def _noop_backend(*args, **kwargs):
    """Stand-in for _ensure_memory_backend — never builds a real container/model."""
    return None


@pytest.fixture(autouse=True)
def _stub_container(monkeypatch):
    """Patch the container build for ALL tests so no real embedder/db is loaded.

    Each test still patches _kb_enabled and the engine/router it exercises.
    """
    monkeypatch.setattr("cli.commands.kb._ensure_memory_backend", _noop_backend)


# ---------------------------------------------------------------------------
# kb add — happy path
# ---------------------------------------------------------------------------

def test_kb_add_reports_ingested(tmp_path, monkeypatch):
    """kb add <dir> prints chunk count and file count on success."""
    # Arrange: a small tmp dir with one markdown file
    (tmp_path / "notes.md").write_text("# Hello\nThis is a test note.")

    # Stub _bootstrap so it doesn't touch the real filesystem
    monkeypatch.setattr("cli.commands.kb._bootstrap", lambda: None)
    # KB enabled
    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: True)
    # Stub the engine
    import cli.commands.kb as kb_mod
    monkeypatch.setattr(
        "tools.knowledge_ingest.kb_ingest",
        _async_return({
            "ingested": 1,
            "unchanged": 0,
            "n_chunks": 3,
            "skipped_secret": 0,
            "skipped_binary": 0,
            "skipped_office": 0,
        }),
    )

    runner = CliRunner()
    result = runner.invoke(kb, ["add", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "1" in result.output          # ingested file count
    assert "3" in result.output          # chunk count


def test_kb_add_calls_engine_with_right_args(tmp_path, monkeypatch):
    """kb add passes --collection and --no-recursive to kb_ingest."""
    (tmp_path / "doc.txt").write_text("hello")

    monkeypatch.setattr("cli.commands.kb._bootstrap", lambda: None)
    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: True)

    calls = []

    async def _fake_ingest(path, collection="default", recursive=True, globs=None,
                           *, user_id, session_id):
        calls.append({
            "path": path, "collection": collection,
            "recursive": recursive, "globs": globs,
            "user_id": user_id,
        })
        return {"ingested": 1, "unchanged": 0, "n_chunks": 2,
                "skipped_secret": 0, "skipped_binary": 0, "skipped_office": 0}

    monkeypatch.setattr("tools.knowledge_ingest.kb_ingest", _fake_ingest)

    runner = CliRunner()
    result = runner.invoke(
        kb,
        ["add", str(tmp_path), "--collection", "docs", "--no-recursive"],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["collection"] == "docs"
    assert calls[0]["recursive"] is False
    assert calls[0]["user_id"] == "local"


# ---------------------------------------------------------------------------
# kb add — engine returns error
# ---------------------------------------------------------------------------

def test_kb_add_surfaces_engine_refusal(monkeypatch):
    """kb add shows a clear message when kb_ingest returns an error key."""
    monkeypatch.setattr("cli.commands.kb._bootstrap", lambda: None)
    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: True)
    monkeypatch.setattr(
        "tools.knowledge_ingest.kb_ingest",
        _async_return({
            "error": "Path '/etc' is outside the allowed workspace root",
            "ingested": 0, "unchanged": 0, "n_chunks": 0,
            "skipped_secret": 0, "skipped_binary": 0, "skipped_office": 0,
        }),
    )

    runner = CliRunner()
    result = runner.invoke(kb, ["add", "/etc"])

    # Non-zero exit and the error message is in output (no raw traceback)
    assert result.exit_code != 0
    assert "outside the allowed workspace root" in result.output
    assert "Traceback" not in result.output


def test_kb_add_surfaces_engine_exception(monkeypatch):
    """kb add handles a raised exception gracefully (no traceback to user)."""
    monkeypatch.setattr("cli.commands.kb._bootstrap", lambda: None)
    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: True)

    async def _explode(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("tools.knowledge_ingest.kb_ingest", _explode)

    runner = CliRunner()
    result = runner.invoke(kb, ["add", "."])

    assert result.exit_code != 0
    assert "disk full" in result.output


def test_kb_add_surfaces_confinement_error(monkeypatch):
    """A realistic confinement RAISE (not error-dict) surfaces cleanly, no traceback."""
    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: True)

    async def _refuse(*a, **kw):
        raise PermissionError("outside allowed root")

    monkeypatch.setattr("tools.knowledge_ingest.kb_ingest", _refuse)

    runner = CliRunner()
    result = runner.invoke(kb, ["add", "/etc/passwd"])

    assert result.exit_code != 0
    assert "outside allowed root" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# kb search — happy path
# ---------------------------------------------------------------------------

def test_kb_search_prints_snippet(monkeypatch):
    """kb search returns the snippet text from the registry router."""
    monkeypatch.setattr("cli.commands.kb._bootstrap", lambda: None)
    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: True)
    monkeypatch.setattr(
        "modules.memory.registry.kb_search",
        _async_return("Source: notes.md\nChunk: This is a test note."),
    )

    runner = CliRunner()
    result = runner.invoke(kb, ["search", "test note"])

    assert result.exit_code == 0, result.output
    assert "test note" in result.output.lower() or "notes.md" in result.output


def test_kb_search_no_results(monkeypatch):
    """kb search prints a friendly message when nothing is found."""
    monkeypatch.setattr("cli.commands.kb._bootstrap", lambda: None)
    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: True)
    monkeypatch.setattr("modules.memory.registry.kb_search", _async_return(""))

    runner = CliRunner()
    result = runner.invoke(kb, ["search", "xyzzy"])

    assert result.exit_code == 0
    assert "no results" in result.output.lower()


# ---------------------------------------------------------------------------
# kb list
# ---------------------------------------------------------------------------

def test_kb_list_prints_sources(monkeypatch):
    monkeypatch.setattr("cli.commands.kb._bootstrap", lambda: None)
    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: True)
    monkeypatch.setattr(
        "modules.memory.registry.kb_list_sources",
        _async_return(["/path/to/notes.md", "/path/to/guide.txt"]),
    )

    runner = CliRunner()
    result = runner.invoke(kb, ["list"])

    assert result.exit_code == 0, result.output
    assert "notes.md" in result.output
    assert "guide.txt" in result.output


def test_kb_list_empty(monkeypatch):
    monkeypatch.setattr("cli.commands.kb._bootstrap", lambda: None)
    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: True)
    monkeypatch.setattr("modules.memory.registry.kb_list_sources", _async_return([]))

    runner = CliRunner()
    result = runner.invoke(kb, ["list"])

    assert result.exit_code == 0
    assert "no sources" in result.output.lower()


# ---------------------------------------------------------------------------
# kb remove
# ---------------------------------------------------------------------------

def test_kb_remove_prints_count(monkeypatch):
    monkeypatch.setattr("cli.commands.kb._bootstrap", lambda: None)
    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: True)
    monkeypatch.setattr("modules.memory.registry.kb_remove", _async_return(5))

    runner = CliRunner()
    result = runner.invoke(kb, ["remove", "--collection", "docs"])

    assert result.exit_code == 0, result.output
    assert "5" in result.output


def test_kb_remove_with_source(monkeypatch):
    monkeypatch.setattr("cli.commands.kb._bootstrap", lambda: None)
    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: True)

    calls = []

    async def _fake_remove(*, user_id, collection, source=None):
        calls.append({"collection": collection, "source": source})
        return 2

    monkeypatch.setattr("modules.memory.registry.kb_remove", _fake_remove)

    runner = CliRunner()
    result = runner.invoke(
        kb, ["remove", "--collection", "docs", "--source", "/path/notes.md"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["source"] == "/path/notes.md"
    assert "2" in result.output


# ---------------------------------------------------------------------------
# KB disabled → friendly message + exit 0 for ALL subcommands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd_args", [
    ["add", "."],
    ["list"],
    ["remove"],
    ["search", "hello"],
])
def test_kb_disabled_friendly_exit(cmd_args, monkeypatch):
    """When KB_ENABLED is False every subcommand prints 'KB disabled' and exits
    NON-zero (2) so a script can tell 'disabled, did nothing' from 'succeeded'."""
    monkeypatch.setattr("cli.commands.kb._bootstrap", lambda: None)
    monkeypatch.setattr("cli.commands.kb._kb_enabled", lambda: False)

    runner = CliRunner()
    result = runner.invoke(kb, cmd_args)

    assert result.exit_code == 2, f"exit_code={result.exit_code} output={result.output}"
    assert "KB disabled" in result.output
    assert "KB disabled" in result.output
    assert "KB_ENABLED" in result.output
