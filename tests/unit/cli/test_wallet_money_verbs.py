"""113 G-1: `polyrob wallet send|swap|check` run the chat verbs' own terminal handlers.

The command adds no authority: it resolves the ``repl`` handler the verb row carries
(``core.money_verbs``), passes the words unchanged (so `go` is the only way to act,
as on the chat seat), and imports no wallet or money code of its own. The agent's
own processes cannot run send or swap (``cli/agent_child_gate.py``).
"""
from pathlib import Path

import pytest
from click.testing import CliRunner

_MODULE = Path(__file__).resolve().parents[3] / "cli" / "commands" / "wallet_verbs.py"


@pytest.fixture
def replies(monkeypatch):
    from cli.commands import wallet_verbs
    monkeypatch.setattr(wallet_verbs, "ensure_env_loaded", lambda: None)
    monkeypatch.setattr("polyrob.app.bootstrap.load_env", lambda *a, **k: None)
    monkeypatch.setattr("core.identity.resolve_identity", lambda: "tenant-a")
    monkeypatch.setattr("cli._admin_home.admin_owner_tenant", lambda uid: uid)
    seen = []

    def _fake(name, text):
        async def _reply(user_id, args, **kw):
            seen.append((name, user_id, list(args)))
            return text
        return _reply

    monkeypatch.setattr("surfaces.telegram.send_ops.send_reply",
                        _fake("send", "quote\nTo send it: /send 1 native to 0xabc on base go"))
    monkeypatch.setattr("surfaces.telegram.swap_ops.swap_reply",
                        _fake("swap", "❌ The quote did not run: boom. Nothing was swapped."))
    monkeypatch.setattr("surfaces.telegram.check_ops.check_reply", _fake("check", "holdings"))
    return seen


def _wallet(*args):
    from cli.commands.wallet import wallet_cmd
    return CliRunner().invoke(wallet_cmd, list(args))


def test_send_runs_the_chat_handler_with_the_words_unchanged(replies):
    res = _wallet("send", "1", "native", "to", "0xabc", "on", "base")
    assert res.exit_code == 0, res.output
    assert replies == [("send", "tenant-a", ["1", "native", "to", "0xabc", "on", "base"])]
    assert "To send it: polyrob wallet send 1 native to 0xabc on base go" in res.output


def test_go_is_passed_through_as_the_one_word_that_acts(replies):
    _wallet("send", "1", "native", "to", "0xabc", "on", "base", "go")
    assert replies[0][2][-1] == "go"


def test_a_refusal_exits_one(replies):
    res = _wallet("swap", "1", "usdc", "to", "weth", "on", "base")
    assert res.exit_code == 1 and "Nothing was swapped" in res.output


def test_check_is_the_chat_check(replies):
    res = _wallet("check", "0xabc")
    assert res.exit_code == 0 and "holdings" in res.output
    assert replies == [("check", "tenant-a", ["0xabc"])]


def test_the_handler_comes_from_the_verb_row(monkeypatch):
    from cli.commands import wallet_verbs
    from core.verbs import handler_ref
    assert handler_ref("repl", "/send") == "cli.ui.commands.h_send:h_send"
    monkeypatch.setattr(wallet_verbs, "ensure_env_loaded", lambda: None)
    monkeypatch.setattr("core.verbs.handler_ref", lambda seat, name: None)
    res = _wallet("send", "1")
    assert res.exit_code != 0 and "no terminal handler" in res.output


def test_the_module_holds_no_money_code_of_its_own():
    text = _MODULE.read_text()
    for needle in ("tools.defi", "core.money", "core.wallet", "tx_guard", "DefiTradeTool"):
        assert f"import {needle}" not in text and f"from {needle}" not in text, needle


def test_the_agent_cannot_send_or_swap_from_its_own_shell():
    from cli import agent_child_gate as gate
    assert gate.is_read_invocation(["wallet", "send", "1", "native", "go"]) is False
    assert gate.is_read_invocation(["wallet", "swap", "1", "a", "to", "b"]) is False
    assert gate.is_read_invocation(["wallet", "check", "0xabc"]) is True
