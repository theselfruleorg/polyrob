"""Tests for polyrob sessions commands."""

import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from click.testing import CliRunner


def test_sessions_command_group_exists():
    """Test that the sessions command group is registered."""
    from cli.polyrob import cli
    assert "sessions" in cli.commands
    assert "session" in cli.commands


def test_sessions_list_has_json_flag():
    """Test that sessions list accepts --json flag."""
    from cli.commands.session import session
    runner = CliRunner()

    mock_container = MagicMock()
    mock_agent = MagicMock()
    mock_agent.session_manager.get_all_sessions.return_value = []
    mock_container.get_agent.return_value = mock_agent

    async def _fake_build(*args, **kwargs):
        return mock_container

    # build_cli_container is a function-local import from core.bootstrap, so it must
    # be patched at its definition site (there is no module-level alias in
    # cli.commands.session to patch).
    with patch("core.bootstrap.build_cli_container", side_effect=_fake_build):
        result = runner.invoke(session, ["list", "--json"])
        # Should not crash on --json flag
        assert result.exit_code == 0 or "No sessions found" in result.output


def test_session_list_survives_null_task(monkeypatch):
    """A session row with a present-but-None 'task'/'id'/'created_at' must not crash
    `session list`. `.get(k, "")` returns None (not "") when the key is present with
    a null value, and None[:38] raised TypeError — breaking the whole command."""
    from cli.commands.session import session
    runner = CliRunner()

    mock_agent = MagicMock()
    mock_agent.session_manager.get_all_sessions.return_value = [
        {"id": None, "session_id": "sess-xyz9", "status": "running",
         "task": None, "created_at": None},
    ]
    mock_container = MagicMock()
    mock_container.get_agent.return_value = mock_agent

    async def _fake_build(*args, **kwargs):
        return mock_container

    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)
    with patch("core.bootstrap.build_cli_container", side_effect=_fake_build):
        result = runner.invoke(session, ["list"])

    assert result.exception is None, result.exception
    assert result.exit_code == 0, result.output
    assert "sess-xyz9" in result.output  # fell back to session_id when id was None


def test_summarize_llm_usage_aggregates_records(tmp_path):
    # The fallback used to read a never-written usage.json; it must read the real
    # per-call llm_usage_*.json records and aggregate them.
    from cli.commands.session import _summarize_llm_usage
    d = tmp_path / "data" / "llm_usage"
    d.mkdir(parents=True)
    (d / "llm_usage_1.json").write_text('{"token_count": 100, "cost_estimate": 0.01}')
    (d / "llm_usage_2.json").write_text('{"token_count": 50, "cost_estimate": 0.005}')
    s = _summarize_llm_usage(tmp_path)
    assert s["records"] == 2
    assert s["total_tokens"] == 150
    assert round(s["total_cost_estimate"], 4) == 0.015


def test_summarize_llm_usage_none_when_absent(tmp_path):
    from cli.commands.session import _summarize_llm_usage
    assert _summarize_llm_usage(tmp_path) is None


def test_assemble_export_folds_session_metadata(tmp_path):
    # export must include the fetched session metadata (status/model/task), not just
    # session_id/exported_at/dir (the old near-empty payload discarded the fetch).
    from cli.commands.session import _assemble_export_data
    (tmp_path / "message_history.json").write_text('[{"role": "user"}]')
    info = {"status": "completed", "model": "gpt-5", "task": "do x", "created_at": "t0"}
    data = _assemble_export_data("sess1", info, tmp_path, "2026-01-01T00:00:00")
    assert data["session"]["status"] == "completed"
    assert data["session"]["model"] == "gpt-5"
    assert data["messages"] == [{"role": "user"}]
    assert data["session_id"] == "sess1"


def test_sessions_show_command_exists():
    """Test that sessions show command exists."""
    from cli.commands.session import session
    assert "show" in session.commands


def test_sessions_artifacts_command_exists():
    """Test that sessions artifacts command exists."""
    from cli.commands.session import session
    assert "artifacts" in session.commands


def test_sessions_costs_command_exists():
    """Test that sessions costs command exists."""
    from cli.commands.session import session
    assert "costs" in session.commands


def test_sessions_tools_command_exists():
    """Test that sessions tools command exists."""
    from cli.commands.session import session
    assert "tools" in session.commands


def test_session_show_uses_get_session_info():
    """`session show` must call get_session_info (SessionManager has no get_session)."""
    from cli.commands.session import session
    runner = CliRunner()

    class FakeSM:
        def __init__(self):
            self.called_with = None

        def get_session_info(self, sid):
            self.called_with = sid
            return {"id": sid, "task": "demo", "status": "running", "created_at": "t0"}
        # deliberately NO get_session attribute — mirrors the real SessionManager

    fake_sm = FakeSM()
    fake_agent = type("A", (), {"session_manager": fake_sm})()
    mock_container = MagicMock()
    mock_container.get_agent.return_value = fake_agent

    async def _fake_build(*args, **kwargs):
        return mock_container

    with patch("core.bootstrap.build_cli_container", side_effect=_fake_build):
        result = runner.invoke(session, ["show", "abc123", "--json"])

    assert result.exit_code == 0, result.output
    assert fake_sm.called_with == "abc123"
