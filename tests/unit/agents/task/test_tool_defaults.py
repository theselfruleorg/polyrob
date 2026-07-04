import importlib


def test_coding_on_under_local_mode(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.delenv("CODING_TOOLS_ENABLED", raising=False)
    import tools.coding as coding
    importlib.reload(coding)
    assert coding.coding_tools_enabled() is True


def test_coding_off_on_server(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("CODING_TOOLS_ENABLED", raising=False)
    import tools.coding as coding
    importlib.reload(coding)
    assert coding.coding_tools_enabled() is False


def test_explicit_disable_wins_under_local(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.setenv("CODING_TOOLS_ENABLED", "false")
    import tools.coding as coding
    importlib.reload(coding)
    assert coding.coding_tools_enabled() is False


def test_server_default_tools_stable():
    from agents.task.tool_defaults import server_default_tools
    # web_fetch is the default web reader; browser is opt-in (not a default).
    assert server_default_tools() == ['filesystem', 'task', 'web_fetch', 'perplexity', 'email', 'mcp', 'anysite']
    assert 'browser' not in server_default_tools()


def test_anysite_in_server_default():
    from agents.task.tool_defaults import server_default_tools
    assert "anysite" in server_default_tools()


def test_anysite_in_cli_default_when_enabled(monkeypatch):
    monkeypatch.delenv("ANYSITE_TOOL_ENABLED", raising=False)
    from agents.task.tool_defaults import cli_default_tools
    assert "anysite" in cli_default_tools()


def test_cli_default_includes_coding_under_local(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.delenv("CODING_TOOLS_ENABLED", raising=False)
    import tools.coding, importlib
    importlib.reload(tools.coding)
    from agents.task.tool_defaults import cli_default_tools
    tl = cli_default_tools()
    assert tl[:2] == ['filesystem', 'task']
    assert 'coding' in tl
    # never includes a non-CLI-registerable tool
    assert 'browser' not in tl


def test_cli_default_plain_when_coding_off(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("CODING_TOOLS_ENABLED", raising=False)
    import tools.coding, importlib
    importlib.reload(tools.coding)
    from agents.task.tool_defaults import cli_default_tools
    tl = cli_default_tools()
    assert tl[:2] == ['filesystem', 'task']
    assert 'coding' not in tl


def test_orchestrator_uses_server_default(monkeypatch):
    import agents.task.agent.orchestrator as orch
    from agents.task.tool_defaults import server_default_tools  # noqa: F401
    # the module should reference the helper, not a literal list
    import inspect
    src = inspect.getsource(orch.SessionOrchestrator.initialize)
    assert "server_default_tools" in src
