"""The REPL `/mcp` was read-only — it could show servers, never add one.

So the terminal seat had the same gap as the phone: the only way to give the
agent a new MCP server was to edit a file on the box. `add`/`remove`/`test`
delegate to `core.mcp_admin`, the ONE helper set every owner seat renders, so
the REPL and Telegram can never disagree about which servers exist.
"""
import asyncio
from types import SimpleNamespace

import pytest


def _ctx(args):
    emitted = []
    ctx = SimpleNamespace(
        args=args,
        agent=None,
        container=None,
        user_id="owner1",
        emit=lambda body, title=None: emitted.append(body),
    )
    return ctx, emitted


def _run(ctx):
    from cli.ui.commands.h_mcp import h_mcp
    asyncio.run(h_mcp(ctx))


def _recorder(seen, reply):
    def _fake(user_id, args, **kw):
        seen["call"] = (user_id, list(args))
        return reply
    return _fake


def test_add_delegates_to_the_shared_helper(monkeypatch):
    seen = {}
    monkeypatch.setattr("core.mcp_admin.mcp_reply", _recorder(seen, "added it"))
    ctx, out = _ctx(["add", "aave", "https://mcp.aave.com"])
    _run(ctx)

    assert seen["call"] == ("owner1", ["add", "aave", "https://mcp.aave.com"])
    assert out == ["added it"]


def test_remove_delegates_to_the_shared_helper(monkeypatch):
    seen = {}
    monkeypatch.setattr("core.mcp_admin.mcp_reply", _recorder(seen, "gone"))
    ctx, out = _ctx(["remove", "aave"])
    _run(ctx)

    assert seen["call"] == ("owner1", ["remove", "aave"])
    assert out == ["gone"]


def test_test_delegates_to_the_shared_helper(monkeypatch):
    seen = {}
    monkeypatch.setattr("core.mcp_admin.mcp_reply", _recorder(seen, "answered"))
    ctx, out = _ctx(["test", "aave"])
    _run(ctx)

    assert seen["call"] == ("owner1", ["test", "aave"])


def test_bare_and_list_keep_the_read_only_view(monkeypatch):
    """The existing status view must not regress into the admin renderer."""
    monkeypatch.setattr("core.mcp_admin.mcp_reply",
                        lambda uid, a, **kw: pytest.fail("list must not go to the admin helper"))
    ctx, out = _ctx([])
    _run(ctx)
    assert out, "the read-only listing must still render"


def test_an_unknown_subcommand_names_the_real_verbs():
    ctx, out = _ctx(["frobnicate"])
    _run(ctx)
    assert out and "add" in out[0] and "remove" in out[0]
