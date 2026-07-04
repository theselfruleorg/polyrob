"""Tests for context-reference expansion wired into the three new call sites.

Covers:
  A. AutonomyConfig.context_references_enabled() helper + server byte-identical guard
  B. REPL _conversation_loop: expands when ON, untouched when OFF
  C. HITL submit_user_message: trusted kinds expand; forged kinds skip
  D. _chat_once_locked: expands text when flag ON
"""
import os
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# A. AutonomyConfig.context_references_enabled() + server guard
# ---------------------------------------------------------------------------

def test_context_references_enabled_default_off(monkeypatch):
    """With no POLYROB_LOCAL and env unset → False (server byte-identical)."""
    monkeypatch.delenv("CONTEXT_REFERENCES_ENABLED", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    from agents.task.constants import AutonomyConfig
    assert AutonomyConfig.context_references_enabled() is False


def test_context_references_enabled_via_env(monkeypatch):
    """Explicit CONTEXT_REFERENCES_ENABLED=true turns it on regardless of POLYROB_LOCAL."""
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("CONTEXT_REFERENCES_ENABLED", "true")
    from agents.task.constants import AutonomyConfig
    assert AutonomyConfig.context_references_enabled() is True


def test_context_references_enabled_via_local_mode(monkeypatch):
    """POLYROB_LOCAL=true defaults CONTEXT_REFERENCES_ENABLED to ON."""
    monkeypatch.delenv("CONTEXT_REFERENCES_ENABLED", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    from agents.task.constants import AutonomyConfig
    assert AutonomyConfig.context_references_enabled() is True


def test_context_references_explicit_off_wins_over_local(monkeypatch):
    """Explicit CONTEXT_REFERENCES_ENABLED=false wins even under POLYROB_LOCAL."""
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.setenv("CONTEXT_REFERENCES_ENABLED", "false")
    from agents.task.constants import AutonomyConfig
    assert AutonomyConfig.context_references_enabled() is False


# ---------------------------------------------------------------------------
# B. REPL _conversation_loop
# ---------------------------------------------------------------------------

def _eof_reader(lines):
    it = iter(lines)

    async def read_line():
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    return read_line


@pytest.mark.asyncio
async def test_repl_expands_file_ref_when_flag_on(tmp_path, monkeypatch):
    """@file:<path> in a REPL line is expanded when CONTEXT_REFERENCES_ENABLED is on."""
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("CONTEXT_REFERENCES_ENABLED", "true")

    # Create a real file so the expansion can inline its content.
    content = "hello from file"
    f = tmp_path / "note.txt"
    f.write_text(content)

    received = {}

    from cli.commands.chat import _conversation_loop

    convo = MagicMock()

    async def _capture(line, **kwargs):
        received["line"] = line
        return "ok"

    convo.respond = AsyncMock(side_effect=_capture)

    with patch("os.getcwd", return_value=str(tmp_path)):
        await _conversation_loop(
            convo,
            MagicMock(),
            read_line=_eof_reader([f"@file:note.txt"]),
        )

    assert "line" in received
    assert content in received["line"], f"Expected file content in: {received['line']}"


@pytest.mark.asyncio
async def test_repl_does_not_expand_when_flag_off(tmp_path, monkeypatch):
    """When CONTEXT_REFERENCES_ENABLED is off, @file tokens are left unchanged."""
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("CONTEXT_REFERENCES_ENABLED", "false")

    f = tmp_path / "note.txt"
    f.write_text("should not appear")

    received = {}

    from cli.commands.chat import _conversation_loop

    convo = MagicMock()

    async def _capture(line, **kwargs):
        received["line"] = line
        return "ok"

    convo.respond = AsyncMock(side_effect=_capture)

    with patch("os.getcwd", return_value=str(tmp_path)):
        await _conversation_loop(
            convo,
            MagicMock(),
            read_line=_eof_reader(["@file:note.txt"]),
        )

    assert received.get("line") == "@file:note.txt"


@pytest.mark.asyncio
async def test_repl_expansion_fails_soft(monkeypatch):
    """If preprocess_context_references raises, the line is passed unchanged."""
    monkeypatch.setenv("CONTEXT_REFERENCES_ENABLED", "true")

    received = {}

    from cli.commands.chat import _conversation_loop

    convo = MagicMock()

    async def _capture(line, **kwargs):
        received["line"] = line
        return "ok"

    convo.respond = AsyncMock(side_effect=_capture)

    def _boom(*a, **kw):
        raise RuntimeError("simulated expansion error")

    with patch(
        "agents.task.agent.messages.context_references.preprocess_context_references",
        side_effect=_boom,
    ):
        await _conversation_loop(
            convo,
            MagicMock(),
            read_line=_eof_reader(["@file:anything"]),
        )

    assert received.get("line") == "@file:anything"


# ---------------------------------------------------------------------------
# C. HITL submit_user_message — forged-kind skip guard
# ---------------------------------------------------------------------------

class _FakeOrchestrator:
    """Minimal stand-in for SessionOrchestrator with HITLIngressMixin."""

    session_id = "test-session-42"
    user_id = "test-user"
    logger = MagicMock()
    agents = {}
    _pending_messages = []
    _pending_messages_lock = None

    async def _get_lock(self):
        import asyncio
        if self._pending_messages_lock is None:
            self._pending_messages_lock = asyncio.Lock()
        return self._pending_messages_lock


def _make_hitl_orchestrator():
    import asyncio
    from agents.task.session.hitl_ingress import HITLIngressMixin

    class _Orch(HITLIngressMixin):
        session_id = "test-session-42"
        user_id = "test-user"
        logger = MagicMock()
        agents = {}
        _pending_messages = []

        def __init__(self):
            import asyncio
            self._pending_messages_lock = asyncio.Lock()

    return _Orch()


async def _run_hitl_submit(tmp_path, *, kind, text):
    """Submit ``text`` with ``kind`` through a fake orchestrator; return queued text.

    The workspace root is pointed at ``tmp_path`` via a patched pm(), so a
    ``@file:`` reference resolves to a real file the test created there.
    """
    orch = _make_hitl_orchestrator()
    mock_pm = MagicMock()
    mock_pm.get_workspace_dir.return_value = tmp_path

    captured = {}

    async def _fake_queue(text_, kind_, metadata_):
        captured["text"] = text_

    orch.agents = {"a": MagicMock(
        hitl_manager=MagicMock(
            get_queue_size=MagicMock(return_value=0),
            queue_user_message=AsyncMock(side_effect=_fake_queue),
        )
    )}

    with patch("agents.task.path.pm", return_value=mock_pm):
        await orch.submit_user_message(agent_id=None, text=text, kind=kind)

    return captured.get("text")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["comment", "continuation"])
async def test_hitl_trusted_kinds_expand(tmp_path, monkeypatch, kind):
    """Allowlisted human-intake kinds (comment, continuation) expand when flag ON."""
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("CONTEXT_REFERENCES_ENABLED", "true")

    content = "hitl file content"
    (tmp_path / "doc.txt").write_text(content)

    queued = await _run_hitl_submit(tmp_path, kind=kind, text="@file:doc.txt")
    assert content in (queued or ""), (
        f"kind={kind!r} should expand; got: {queued!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["self_wake", "delegation_result", "system"])
async def test_hitl_non_allowlisted_kinds_not_expanded(tmp_path, monkeypatch, kind):
    """Forged kinds AND any unlisted kind (e.g. 'system') must NOT expand, flag ON.

    This is the allowlist guarantee: only comment/continuation expand; everything
    else — including a future non-human 'system' kind — is left byte-for-byte.
    """
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("CONTEXT_REFERENCES_ENABLED", "true")

    # Create the file so the ONLY reason it wouldn't expand is the kind gate.
    (tmp_path / "secret.txt").write_text("secret file content")

    original_msg = "@file:secret.txt"
    queued = await _run_hitl_submit(tmp_path, kind=kind, text=original_msg)
    assert queued == original_msg, (
        f"kind={kind!r} must NOT be expanded, got: {queued!r}"
    )


@pytest.mark.asyncio
async def test_hitl_flag_off_not_expanded(tmp_path, monkeypatch):
    """With flag OFF, even a trusted kind leaves text unchanged."""
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("CONTEXT_REFERENCES_ENABLED", "false")

    (tmp_path / "doc.txt").write_text("should not appear")

    original_msg = "@file:doc.txt"
    queued = await _run_hitl_submit(tmp_path, kind="comment", text=original_msg)
    assert queued == original_msg


# ---------------------------------------------------------------------------
# D. _chat_once_locked (HTTP chat_once call site)
# ---------------------------------------------------------------------------

def _make_chat_agent_stub(tmp_path):
    """Minimal TaskAgent stub exercising _chat_once_locked's NEW-session path.

    We mock everything past the expansion seam and capture the task text that
    reaches create_session (the SessionRequest the agent will run on).
    """
    from agents.task_agent_lite import TaskAgent

    agent = TaskAgent.__new__(TaskAgent)
    agent._chat_sessions = {}
    agent.session_manager = MagicMock()
    agent.session_manager.get_session_info.return_value = None
    agent._registry = MagicMock()
    agent._registry.get.return_value = None

    captured = {}

    async def _resolve_persona():
        return None
    agent._resolve_chat_persona = _resolve_persona

    def _tool_ids():
        return ["filesystem"]
    agent._chat_tool_ids = _tool_ids

    async def _create_session(user_id, req, chat_session_key=None, skip_credit_check=False):
        captured["task"] = req.task
        return {"id": "sess-1"}
    agent.create_session = _create_session

    async def _run_session(user_id, session_id):
        return None
    agent.run_session = _run_session

    def _extract(session_id):
        return "reply"
    agent._extract_chat_reply = _extract

    return agent, captured


@pytest.mark.asyncio
async def test_chat_once_expands_when_flag_on(tmp_path, monkeypatch):
    """A @file ref in chat_once input is expanded before reaching create_session."""
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("CONTEXT_REFERENCES_ENABLED", "true")

    content = "chat once file content"
    (tmp_path / "doc.txt").write_text(content)

    agent, captured = _make_chat_agent_stub(tmp_path)

    # New session → no session_id yet → expansion roots at CWD; point CWD at tmp_path.
    with patch("os.getcwd", return_value=str(tmp_path)):
        reply = await agent._chat_once_locked("user-1", "@file:doc.txt", "key-1")

    assert reply == "reply"
    assert content in captured.get("task", ""), (
        f"Expected expanded file content in task: {captured.get('task')!r}"
    )


@pytest.mark.asyncio
async def test_chat_once_not_expanded_when_flag_off(tmp_path, monkeypatch):
    """With the flag OFF, chat_once leaves the @file token untouched."""
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("CONTEXT_REFERENCES_ENABLED", "false")

    (tmp_path / "doc.txt").write_text("should not appear")

    agent, captured = _make_chat_agent_stub(tmp_path)

    with patch("os.getcwd", return_value=str(tmp_path)):
        await agent._chat_once_locked("user-1", "@file:doc.txt", "key-1")

    assert captured.get("task") == "@file:doc.txt"
