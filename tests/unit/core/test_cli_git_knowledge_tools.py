"""SB-02 / SB-09: git/github must be CLI-registerable, and the REPL must add the
knowledge tool to its loaded tool_ids.

SB-02: the git tool was in _SAFE_LOCAL_FLAGS (ON under POLYROB_LOCAL) but registered
NOWHERE — no register_cli_tools block and dead on the server init path — so an
advertised, safety-engineered capability was 100% dead code. github likewise.

SB-09: the knowledge tool is service-registered under KB_ENABLED (local-ON) but was
never appended to the REPL session's tool_ids, so agent-driven kb_ingest was unreachable.
"""
import inspect

import core.bootstrap as bootstrap
import cli.commands.chat as chat


def test_git_and_github_in_cli_registerable_tools():
    assert "git" in bootstrap._CLI_REGISTERABLE_TOOLS
    assert "github" in bootstrap._CLI_REGISTERABLE_TOOLS


def test_register_cli_tools_registers_git():
    src = inspect.getsource(bootstrap.register_cli_tools)
    assert "GitTool" in src
    assert '"git"' in src
    assert "git_enabled" in src


def test_register_cli_tools_registers_github_behind_flag():
    src = inspect.getsource(bootstrap.register_cli_tools)
    assert "GitHubTool" in src
    assert "github_enabled" in src


def test_repl_adds_knowledge_tool_when_kb_enabled():
    # SB-09: the REPL start path must append 'knowledge' to repl_tools when KB is on and
    # the service is available.
    # The append lives in the chat REPL start function; assert the wiring is present.
    import cli.commands.chat as chat_mod
    src = inspect.getsource(chat_mod)
    assert 'repl_tools.append("knowledge")' in src
    assert "kb_enabled()" in src
