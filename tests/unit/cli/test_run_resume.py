"""`polyrob run --resume <id>` continues an existing session (B9.1)."""
from unittest.mock import MagicMock

from click.testing import CliRunner


def test_run_requires_task_xor_resume():
    from cli.commands.run import run
    r = CliRunner()
    # neither TASK nor --resume
    res = r.invoke(run, [])
    assert res.exit_code != 0
    assert "either a TASK or --resume" in res.output
    # both
    res = r.invoke(run, ["do a thing", "--resume", "sess1"])
    assert res.exit_code != 0
    assert "either a TASK or --resume" in res.output


def test_run_resume_unknown_session_errors(monkeypatch):
    from cli.commands import run as run_mod

    task_agent = MagicMock()
    task_agent.session_manager.get_session_info.return_value = None  # not found

    container = MagicMock()
    container.get_agent.return_value = task_agent
    container.get_service.return_value = MagicMock(resolve=lambda: "local")

    async def _fake_build(**kwargs):
        return container

    monkeypatch.setattr("core.bootstrap.build_cli_container", _fake_build)
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)

    res = CliRunner().invoke(run_mod.run, ["--resume", "no-such-session"])
    assert res.exit_code == 1, res.output
    assert "not found" in res.output.lower()
    # It must NOT create a new session for a resume.
    task_agent.create_session.assert_not_called()


def test_run_resume_recreates_and_runs(monkeypatch):
    # A known session: resume rehydrates the orchestrator and calls run_session
    # (not create_session).
    from cli.commands import run as run_mod

    task_agent = MagicMock()
    task_agent.session_manager.get_session_info.return_value = {
        "id": "sess-abc", "user_id": "local", "task": "earlier task",
        "model": "gpt-5", "provider": "openai", "status": "suspended",
    }
    task_agent.get_orchestrator.return_value = None  # cold (fresh process)

    async def _recreate(session_id, info):
        return MagicMock()  # a rehydrated orchestrator

    async def _run_session(**kwargs):
        return "done"

    task_agent._recreate_orchestrator = _recreate
    task_agent.run_session = _run_session
    task_agent.create_session = MagicMock()

    container = MagicMock()
    container.get_agent.return_value = task_agent
    container.get_service.return_value = MagicMock(resolve=lambda: "local")

    async def _fake_build(**kwargs):
        return container

    monkeypatch.setattr("core.bootstrap.build_cli_container", _fake_build)
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)

    res = CliRunner().invoke(run_mod.run, ["--resume", "sess-abc", "--plain"])
    assert res.exit_code == 0, res.output
    task_agent.create_session.assert_not_called()
