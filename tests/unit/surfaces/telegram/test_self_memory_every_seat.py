"""113 W6 G-6: `/self` and `/memory search` on every seat — reads only.

The phone could not read the identity docs or search memory (both were
terminal-only rows). The chat table now runs ``self_memory_ops``; the terminal
emits the same replies; a group room refuses both (owner reads).
"""
import asyncio

from core import verbs
from surfaces.telegram import self_memory_ops


class _Provider:
    name = "fake-fts"
    is_external = True

    def __init__(self):
        self.calls = []

    async def search(self, query, *, user_id=None, limit=5):
        self.calls.append((query, user_id, limit))
        return f"hit for {query}"


def _registry(monkeypatch, provider):
    reg = type("R", (), {"active": lambda self: provider})()
    monkeypatch.setattr("modules.memory.registry.get_memory_registry", lambda: reg)


def test_memory_search_reads_the_owner_tenant(monkeypatch):
    p = _Provider()
    _registry(monkeypatch, p)
    out = asyncio.run(self_memory_ops.memory_reply("u_owner", ["search", "the", "plan"]))
    assert out == "hit for the plan"
    assert p.calls == [("the plan", "u_owner", 10)]


def test_memory_search_on_an_unreadable_store_says_so(monkeypatch):
    class Broken(_Provider):
        async def search(self, query, **kw):
            raise OSError("locked")
    _registry(monkeypatch, Broken())
    out = asyncio.run(self_memory_ops.memory_reply("u1", ["search", "x"]))
    assert "could not be read" in out and "nothing was searched" in out


def test_the_chat_table_runs_both_and_a_room_refuses_both():
    from surfaces.telegram.admin_verbs import ADMIN_VERBS as _TABLE
    from surfaces.telegram.harness import _ROOM_REFUSED_COMMANDS
    for name in ("/self", "/memory"):
        assert name in _TABLE
        assert name in _ROOM_REFUSED_COMMANDS and verbs.room_refused(name)
    assert verbs.verb_for("/soul").name == "/self"


def test_self_reply_names_an_unread_document(monkeypatch):
    monkeypatch.setattr("core.instance.load_self_context",
                        lambda home: (_ for _ in ()).throw(OSError("gone")))
    monkeypatch.setattr("core.instance.load_self_doc", lambda home, uid, iid: "my note")
    out = self_memory_ops.self_reply("u1", "/nowhere")
    assert "could not read it: OSError" in out and "my note" in out
