"""`polyrob run --resume <id>` continues an existing session (B9.1)."""
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner


@pytest.fixture
def resolved_provider(monkeypatch):
    """Pin provider/model resolution.

    `run` calls `resolve_provider_model` AFTER the preflight these tests stub,
    and on a box with zero provider keys it returns (None, None) and exits 1
    with the no-key message — before any resume logic runs. That made these
    tests pass only where the developer happened to have a key in the
    environment, and fail on a clean CI runner. Resume routing is the subject
    here; credential resolution has its own tests.
    """
    monkeypatch.setattr(
        "cli.config_store.resolve_provider_model",
        lambda provider, model, **kw: ("openai", "gpt-5"),
    )


def test_run_requires_a_task_or_resume():
    from cli.commands.run import run
    res = CliRunner().invoke(run, [])
    assert res.exit_code != 0
    assert "provide a TASK, or --continue" in res.output


def test_run_resume_unknown_session_errors(monkeypatch, resolved_provider):
    from cli.commands import run as run_mod

    task_agent = MagicMock()
    task_agent.session_manager.get_session_info.return_value = None  # not found

    container = MagicMock()
    container.get_agent.return_value = task_agent
    container.get_service.return_value = MagicMock(resolve=lambda: "local")

    async def _fake_build(**kwargs):
        return container

    monkeypatch.setattr("polyrob.app.bootstrap.build_cli_container", _fake_build)
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)

    res = CliRunner().invoke(run_mod.run, ["--resume", "no-such-session"])
    assert res.exit_code == 1, res.output
    assert "not found" in res.output.lower()
    # It must NOT create a new session for a resume.
    task_agent.create_session.assert_not_called()


def test_run_resume_recreates_and_runs(monkeypatch, resolved_provider):
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

    monkeypatch.setattr("polyrob.app.bootstrap.build_cli_container", _fake_build)
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)

    res = CliRunner().invoke(run_mod.run, ["--resume", "sess-abc", "--plain"])
    assert res.exit_code == 0, res.output
    task_agent.create_session.assert_not_called()


def test_run_resume_with_a_message_queues_it_before_the_run(monkeypatch, resolved_provider):
    """113 W0 (T-01): `--resume ID "message"` continues a completed chat. Without the
    message a completed chat has nothing to run, so the promise in the help was false."""
    from unittest.mock import AsyncMock
    from cli.commands import run as run_mod

    order = []
    orch = MagicMock()
    orch.submit_user_message = AsyncMock(side_effect=lambda **kw: order.append(("queued", kw)))
    task_agent = MagicMock()
    task_agent.session_manager.get_session_info.return_value = {
        "id": "sess-abc", "user_id": "local", "task": "earlier task",
        "model": "gpt-5", "provider": "openai", "status": "completed",
    }
    task_agent.get_orchestrator.return_value = orch

    async def _run_session(**kwargs):
        order.append(("run", kwargs))
        return "done"

    task_agent.run_session = _run_session
    task_agent.create_session = MagicMock()
    container = MagicMock()
    container.get_agent.return_value = task_agent
    container.get_service.return_value = MagicMock(resolve=lambda: "local")

    async def _fake_build(**kwargs):
        return container

    monkeypatch.setattr("polyrob.app.bootstrap.build_cli_container", _fake_build)
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)
    monkeypatch.setattr("core.surfaces.owner_thread.record_owner_in", lambda *a, **k: True)

    res = CliRunner().invoke(run_mod.run, ["and now the tests", "--resume", "sess-abc", "--plain"])
    assert res.exit_code == 0, res.output
    task_agent.create_session.assert_not_called()
    assert [o[0] for o in order] == ["queued", "run"]
    assert order[0][1]["text"] == "and now the tests"
    assert order[0][1]["metadata"] == {"typed_text": "and now the tests"}


def test_continue_is_the_name_and_resume_its_old_alias():
    """113 §2 item 5 (T-03): "resume" means "lift a pause" elsewhere, so the option that
    continues a chat is --continue; --resume still works and its help says it is the old name."""
    from cli.commands.run import run
    opt = next(p for p in run.params if p.name == "resume_id")
    assert opt.opts == ["--continue", "--resume"]
    res = CliRunner().invoke(run, ["--help"])
    assert "--continue, --resume SESSION_ID" in res.output
    assert "old name of --continue" in res.output


def test_continue_and_resume_take_the_same_path(monkeypatch, resolved_provider):
    from cli.commands import run as run_mod
    for flag in ("--continue", "--resume"):
        task_agent = MagicMock()
        task_agent.session_manager.get_session_info.return_value = None
        container = MagicMock()
        container.get_agent.return_value = task_agent
        container.get_service.return_value = MagicMock(resolve=lambda: "local")

        async def _fake_build(**kwargs):
            return container

        monkeypatch.setattr("polyrob.app.bootstrap.build_cli_container", _fake_build)
        monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)
        res = CliRunner().invoke(run_mod.run, [flag, "no-such-session"])
        assert res.exit_code == 1, (flag, res.output)
        assert "not found" in res.output.lower()
        task_agent.create_session.assert_not_called()
