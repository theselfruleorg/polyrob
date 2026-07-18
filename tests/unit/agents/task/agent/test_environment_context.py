"""<environment> foundation block (proposal 014-C1).

The agent gets told WHERE IT LIVES — instance, platform, data dir, absolute
workspace path + persistence semantics, capability-axis levels, host executable
probe — as a foundation message pinned after runtime identity. Emitted ONLY
under local mode or effective autonomous mode (D-3); a plain multi-tenant
server session is byte-identical (no message).

Flag pattern per test_security_prompt_block.py: patch env / live module attrs,
never importlib.reload(agents.task.constants).
"""


def _local(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))


def test_block_emitted_under_local(monkeypatch, tmp_path):
    _local(monkeypatch, tmp_path)
    from agents.task.agent.core.env_context import build_environment_context
    text = build_environment_context(session_id="s1", user_id="rob")
    assert text and "<environment>" in text and "</environment>" in text
    assert "Workspace" in text
    assert "persist" in text.lower()  # the persistence sentence
    assert "compute posture" in text.lower()


def test_block_absent_on_plain_server(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("AUTONOMY_MODE", raising=False)
    from agents.task.agent.core.env_context import build_environment_context
    assert build_environment_context(session_id="s1", user_id="u_123") is None


def test_block_flag_off_disables(monkeypatch, tmp_path):
    _local(monkeypatch, tmp_path)
    monkeypatch.setenv("ENV_CONTEXT_BLOCK", "false")
    from agents.task.agent.core.env_context import build_environment_context
    assert build_environment_context(session_id="s1", user_id="rob") is None


def test_block_never_contains_secret_shapes(monkeypatch, tmp_path):
    _local(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_API_KEY", "sk-should-never-appear")
    from agents.task.agent.core.env_context import build_environment_context
    text = build_environment_context(session_id="s1", user_id="rob") or ""
    assert "sk-should-never-appear" not in text


def test_shared_project_workspace_wording(monkeypatch, tmp_path):
    _local(monkeypatch, tmp_path)
    monkeypatch.setenv("POLYROB_PROJECT_DIR", str(tmp_path / "proj"))
    from agents.task.agent.core.env_context import build_environment_context
    text = build_environment_context(session_id="s1", user_id="rob") or ""
    assert "across sessions" in text  # shared-workspace persistence wording


def test_message_manager_has_environment_setter():
    from agents.task.agent.message_manager.service import MessageManager
    assert hasattr(MessageManager, "set_environment_message")
    from modules.llm.messages import MessageOrigin
    assert MessageOrigin.ENVIRONMENT == "environment"


def test_retrieval_injects_environment_between_identity_and_self_context():
    # Foundation-order contract without constructing a full MessageManager:
    # both retrieval paths must reference the environment slot, and the
    # injection must sit AFTER runtime_identity and BEFORE self_context
    # (source-introspection style, per test_orchestrator_uses_server_default).
    import inspect
    import agents.task.agent.messages.retrieval as retrieval
    src = inspect.getsource(retrieval)
    assert src.count("_environment_message") >= 2  # get_messages + get_messages_for_llm
    ident = src.index("_runtime_identity_message")
    env = src.index("_environment_message")
    self_ctx = src.index("_self_context_message")
    assert ident < env < self_ctx
