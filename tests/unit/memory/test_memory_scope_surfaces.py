"""025 — the owner read surfaces never leak a quarantine, and the owner can act.

Seeds one shared and one scoped row, then asserts that the console memory body,
the REPL ``/memory scopes`` text and ``polyrob owner memory scopes`` show the
shared view / the counts only, and that promote / purge work from the CLI.
"""
import asyncio
import os

import pytest
from click.testing import CliRunner

from modules.memory import scope as S
from modules.memory.sqlite_memory_provider import SqliteMemoryProvider

USER = "alice"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", USER)
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    monkeypatch.setenv("MEMORY_SCOPES_ENABLED", "true")
    monkeypatch.setenv("MEMORY_STORE_ANSWER_ONLY", "true")
    return tmp_path


def _seed(home) -> SqliteMemoryProvider:
    p = SqliteMemoryProvider(os.path.join(str(home), "memory.db"))

    async def go():
        await p.sync_turn("q", "public pricing note", session_id="chat", user_id=USER)
        await p.sync_turn("q", "quarantined pricing finding", session_id="g", user_id=USER,
                          scope=S.MemoryScopeSpec("goal:abc123", "scoped"))
    asyncio.run(go())
    return p


def test_console_memory_body_is_the_shared_view(home, monkeypatch):
    p = _seed(home)
    import webview.pages as pages
    monkeypatch.setattr(pages, "_memory_provider_status", lambda: (p, None))
    from webview.pages_new import _memory_search_body
    body = asyncio.run(_memory_search_body(USER, "pricing", 10))
    text = " ".join(body["recall"]) if isinstance(body["recall"], list) else str(body["recall"])
    assert "public pricing note" in text and "quarantined" not in text
    assert body["held_in_scopes"] == {"enabled": True, "scopes": 1, "rows": 1}


def test_repl_memory_scopes_names_counts_not_findings(home):
    p = _seed(home)
    from cli.ui.commands.handlers import _memory_scopes_text
    out = _memory_scopes_text(p, USER)
    assert "goal:abc123" in out and "1 row(s)" in out and "quarantined pricing" not in out
    assert "polyrob owner memory scopes" in out


def test_owner_cli_list_show_promote_purge(home):
    _seed(home)
    from cli.commands.owner import owner
    r = CliRunner()
    out = r.invoke(owner, ["memory", "scopes", "list"]).output
    assert "goal:abc123" in out and "memory scopes: ON" in out
    out = r.invoke(owner, ["memory", "scopes", "show", "goal:abc123"]).output
    assert "quarantined pricing finding" in out
    out = r.invoke(owner, ["memory", "scopes", "promote", "goal:abc123"]).output
    assert "promoted 1 finding" in out
    out = r.invoke(owner, ["memory", "scopes", "list"]).output
    assert "no quarantined memory scopes" in out


def test_owner_cli_purge(home):
    p = _seed(home)
    from cli.commands.owner import owner
    out = CliRunner().invoke(owner, ["memory", "scopes", "purge", "goal:abc123", "--yes"]).output
    assert "purged 1 finding" in out
    assert p.list_scopes(USER) == []


def test_owner_cli_names_a_missing_store(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", USER)
    from cli.commands.owner import owner
    out = CliRunner().invoke(owner, ["memory", "scopes", "list"]).output
    assert out.startswith("unavailable(")
