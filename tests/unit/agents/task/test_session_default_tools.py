"""Session-entry default toolset SSOT (proposal 014-A1).

A bare SessionRequest historically hardcoded ['browser','filesystem','task'] in
three places in agents/task_agent_lite.py (:74, :424, :2071) — none mode-aware,
none guarded by a test. default_session_tools() is the one answer now.

Env-flag pattern per tests/unit/agents/task/test_autonomous_toolset.py — patch
env, never importlib.reload(agents.task.constants) (AutonomyConfig rebind
landmine).
"""


def _enable_full(monkeypatch):
    monkeypatch.setenv("AUTONOMY_MODE", "autonomous")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")


def test_supervised_session_default_byte_identical(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    from agents.task.tool_defaults import default_session_tools
    assert default_session_tools() == ["browser", "filesystem", "task"]


def test_autonomous_session_default_widens(monkeypatch):
    _enable_full(monkeypatch)
    from agents.task.tool_defaults import default_session_tools
    tools = default_session_tools()
    for t in ("web_fetch", "coding", "email", "anysite"):
        assert t in tools
    for t in ("goal", "cronjob"):  # ambient sessions: no meta tools
        assert t not in tools
    for t in ("x402_pay", "hyperliquid", "polymarket", "code_execution", "shell"):
        assert t not in tools  # money-spend + compute NEVER via mode


def test_session_request_post_init_uses_ssot(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    from agents.task_agent_lite import SessionRequest
    req = SessionRequest(task="t")
    assert req.tools == ["browser", "filesystem", "task"]
    _enable_full(monkeypatch)
    req2 = SessionRequest(task="t")
    assert "coding" in req2.tools and "browser" in req2.tools


def test_session_request_defaults_route_through_ssot():
    import inspect
    import agents.task_agent_lite as tal
    src = inspect.getsource(tal)
    # the three historical literals must be gone (SSOT contract; mirrors the
    # source-introspection style of test_orchestrator_uses_server_default)
    assert src.count('["browser", "filesystem", "task"]') == 0
    assert src.count("['browser', 'filesystem', 'task']") == 0
    assert "default_session_tools" in src
