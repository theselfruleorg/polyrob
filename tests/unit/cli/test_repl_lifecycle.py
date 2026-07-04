"""REPL lifecycle hardening tests (audit F1-F9).

Covers:
  F1/F8 — a create_session session-limit error renders the actionable block.
  F2    — a SIGINT-torn REPL exit still flips the session to a terminal status.
  F3    — cancel_session flips the persisted status even when the orchestrator's
          cancel() does not touch session status.
  F5    — a crashing slash handler yields a styled error, not a propagated
          exception; the REPL continues.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# F1 / F8 — actionable session-limit message
# ---------------------------------------------------------------------------


def test_session_limit_message_is_actionable():
    from cli.commands._errors import session_limit_message, is_session_limit_error

    msg = session_limit_message("local")
    assert "Session limit reached (user 'local')." in msg
    assert "polyrob session list" in msg
    assert "polyrob session cancel <id>" in msg
    assert "MAX_SESSIONS_PER_USER=60 polyrob" in msg

    assert is_session_limit_error(Exception("Session limit reached for user local."))
    assert not is_session_limit_error(Exception("some other error"))


def test_echo_create_session_error_renders_limit_block(capsys):
    """A session-limit AgentError renders the F8 block (not the bare message)."""
    from cli.commands._errors import echo_create_session_error
    from core.exceptions import AgentError

    echo_create_session_error(
        AgentError("Session limit reached for user local. Please complete..."),
        "local",
    )
    out = capsys.readouterr().out
    assert "[polyrob] ERROR:" in out
    assert "polyrob session list" in out
    assert "MAX_SESSIONS_PER_USER=60 polyrob" in out


def test_echo_create_session_error_passes_through_other_errors(capsys):
    from cli.commands._errors import echo_create_session_error

    echo_create_session_error(RuntimeError("boom: db unreachable"), "local")
    out = capsys.readouterr().out
    assert "boom: db unreachable" in out
    assert "polyrob session list" not in out


@pytest.mark.asyncio
async def test_repl_create_session_limit_renders_block(monkeypatch, capsys, tmp_path):
    """Driving _repl_main with a task_agent.create_session that raises the
    session-limit AgentError renders the actionable block and returns cleanly
    (no traceback escapes)."""
    from core.exceptions import AgentError

    # Stub the bootstrap so no real container is built.
    monkeypatch.setattr("core.bootstrap.setup_project_path", lambda: None)
    monkeypatch.setattr("core.bootstrap.setup_sqlite_compat", lambda: None)
    monkeypatch.setattr("core.bootstrap.load_env", lambda **k: None)

    task_agent = MagicMock()
    task_agent.create_session = AsyncMock(
        side_effect=AgentError("Session limit reached for user local. Please complete...")
    )

    container = MagicMock()
    container.get_agent.return_value = task_agent
    container.get_service.return_value = MagicMock(resolve=lambda: "local")

    async def _fake_build(**k):
        return container

    monkeypatch.setattr("core.bootstrap.build_cli_container", _fake_build)
    # Ensure the onboarding-hint short-circuit does not fire — must be a usable-length
    # key (>=20 chars) now that the gating oracle rejects too-short/placeholder keys.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "a" * 30)
    # Keep prompt_toolkit out of the picture: force plain.
    monkeypatch.setenv("POLYROB_PLAIN", "1")
    monkeypatch.chdir(tmp_path)

    from cli.commands.chat import _repl_main

    ref: dict = {}
    await _repl_main(plain=True, lifecycle_ref=ref)

    out = capsys.readouterr().out
    assert "Session limit reached" in out
    assert "polyrob session list" in out
    # No session id was published (creation failed before it).
    assert "session_id" not in ref


# ---------------------------------------------------------------------------
# F2 — SIGINT-torn exit still persists a terminal status
# ---------------------------------------------------------------------------


def test_repl_sync_cleanup_flips_status_via_session_manager():
    """The synchronous fallback flips the session to 'cancelled' through the
    session_manager API when the async cancel could not run."""
    from cli.commands.chat import _repl_sync_cleanup

    sm = MagicMock()
    task_agent = MagicMock()
    task_agent.session_manager = sm

    ref = {"task_agent": task_agent, "user_id": "throwaway", "session_id": "sess-123"}
    _repl_sync_cleanup(ref)

    sm.update_session_status.assert_called_once_with("sess-123", "cancelled")
    assert ref["cleaned"] is True


def test_repl_sync_cleanup_noop_when_already_cleaned():
    from cli.commands.chat import _repl_sync_cleanup

    sm = MagicMock()
    task_agent = MagicMock()
    task_agent.session_manager = sm
    ref = {"task_agent": task_agent, "session_id": "s", "cleaned": True}
    _repl_sync_cleanup(ref)
    sm.update_session_status.assert_not_called()


def test_repl_sync_cleanup_noop_without_session():
    from cli.commands.chat import _repl_sync_cleanup

    # No session_id → nothing to do.
    _repl_sync_cleanup({"task_agent": MagicMock()})
    _repl_sync_cleanup({})  # totally empty


def test_run_repl_keyboardinterrupt_triggers_sync_cleanup(monkeypatch):
    """A KeyboardInterrupt escaping asyncio.run drives the sync fallback, which
    flips the (already-published) session to a terminal status — the F2 leak fix.

    We simulate the real flow: _repl_main publishes task_agent/session_id into
    lifecycle_ref, then a KeyboardInterrupt is raised (mirroring a forced second
    Ctrl-C that tore the loop down before the async cancel ran)."""
    from cli.commands import chat as chat_mod

    sm = MagicMock()
    task_agent = MagicMock()
    task_agent.session_manager = sm

    async def _fake_main(plain=False, lifecycle_ref=None, **kw):
        # Mirror _repl_main: publish lifecycle objects, then a SIGINT tears the
        # loop down before the finally's async cancel can mark it cleaned.
        lifecycle_ref["task_agent"] = task_agent
        lifecycle_ref["user_id"] = "throwaway"
        lifecycle_ref["session_id"] = "sess-leak"
        raise KeyboardInterrupt

    monkeypatch.setattr(chat_mod, "_repl_main", _fake_main)

    chat_mod.run_repl(plain=True)

    sm.update_session_status.assert_called_once_with("sess-leak", "cancelled")


# ---------------------------------------------------------------------------
# F2 integration probe — real session metadata on disk flips to 'cancelled'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f2_session_status_cancelled_on_disk(monkeypatch, tmp_path):
    """End-to-end-ish probe using the REAL SessionManager: create a session,
    simulate the SIGINT-torn exit via the sync cleanup, and assert the on-disk
    metadata status is 'cancelled' (not 'created').

    This exercises the F2 contract through the session_manager API + F3's
    unconditional status flip, without needing a live LLM/container.
    """
    from agents.task.agent.session import SessionManager
    from agents.task.path import pm

    # Point pm() at a throwaway data root so we don't touch real sessions.
    monkeypatch.setenv("POLYROB_DATA_ROOT", str(tmp_path / "data"))
    # pm() may be a singleton already configured; rebuild its data_root if it
    # exposes one. We rely on a fresh user_id to isolate regardless.
    user_id = "f2-throwaway-user"

    sm = SessionManager()
    # Register a CREATED session directly (mirrors create_session's bookkeeping).
    session_id = sm.create_session(user_id=user_id)

    info = sm.get_session_info(session_id)
    assert info is not None
    assert info.get("status") in ("created", "running", "resumed")

    # Simulate the F2 sync fallback path (what run_repl does on a torn exit).
    from cli.commands.chat import _repl_sync_cleanup

    _repl_sync_cleanup(
        {
            "task_agent": MagicMock(session_manager=sm),
            "user_id": user_id,
            "session_id": session_id,
        }
    )

    # Re-read from the manager (which persists metadata to disk on update).
    info2 = sm.get_session_info(session_id)
    assert info2.get("status") == "cancelled", info2


# ---------------------------------------------------------------------------
# F3 — cancel_session flips status even when orchestrator.cancel() doesn't
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_session_flips_status_unconditionally():
    """A running session (orchestrator present) whose cancel() does NOT touch
    the persisted status still ends up 'cancelled' after cancel_session."""
    from agents.task_agent_lite import TaskAgent

    agent = TaskAgent.__new__(TaskAgent)  # bypass __init__
    agent.task_available = True

    # Stub session_manager.
    sm = MagicMock()
    sm.get_session_info.return_value = {"user_id": "u", "status": "created"}
    agent.session_manager = sm

    # Stub registry returning an orchestrator whose cancel() only flips an
    # in-memory flag (does NOT update session status).
    orch = MagicMock()
    orch.cancel = MagicMock()  # no-op w.r.t. status
    registry = MagicMock()
    registry.get.return_value = orch
    agent._registry = registry

    ok = await agent.cancel_session(user_id="u", session_id="sess-x")
    assert ok is True
    orch.cancel.assert_called_once()
    sm.update_session_status.assert_called_once_with("sess-x", "cancelled")


# ---------------------------------------------------------------------------
# F5 — crashing slash handler → styled error, REPL continues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_handler_crash_is_caught(capsys):
    from cli.ui.commands import Command, CommandContext, CommandRegistry

    reg = CommandRegistry()

    def _boom(ctx):
        raise RuntimeError("handler exploded")

    reg.register(Command("boom", _boom, "explodes"))

    handled = await reg.dispatch("/boom", CommandContext(registry=reg))
    # Still "handled" (not routed as a turn), no exception propagated.
    assert handled is True
    out = capsys.readouterr().out
    assert "command error" in out
    assert "handler exploded" in out


@pytest.mark.asyncio
async def test_dispatch_replexit_still_propagates():
    """ReplExit is control flow and MUST escape dispatch (so /exit works)."""
    from cli.ui.commands import Command, CommandContext, CommandRegistry, ReplExit

    reg = CommandRegistry()

    def _exit(ctx):
        raise ReplExit()

    reg.register(Command("bye", _exit, "leave"))

    with pytest.raises(ReplExit):
        await reg.dispatch("/bye", CommandContext(registry=reg))


# ---------------------------------------------------------------------------
# F7 — `rob skills validate <unknown>` fails clearly (no such skill, exit 1)
# ---------------------------------------------------------------------------


def test_skills_validate_unknown_id_errors(monkeypatch):
    from click.testing import CliRunner

    import cli.commands.skills as skills_mod

    mgr = MagicMock()
    mgr._ensure_rules_loaded = MagicMock()
    mgr.skill_rules = {"known-skill": object()}
    monkeypatch.setattr(skills_mod, "get_skill_manager", lambda: mgr)

    result = CliRunner().invoke(skills_mod.skills, ["validate", "does-not-exist"])
    assert result.exit_code == 1
    assert "no such skill" in result.output
    # Did not attempt to validate the bogus id.
    mgr.validate_skill.assert_not_called()


def test_skills_validate_known_id_runs(monkeypatch):
    from click.testing import CliRunner

    import cli.commands.skills as skills_mod

    res = MagicMock(skill_id="known-skill", is_valid=True, errors=[], warnings=[])
    mgr = MagicMock()
    mgr._ensure_rules_loaded = MagicMock()
    mgr.skill_rules = {"known-skill": object()}
    mgr.validate_skill.return_value = res
    monkeypatch.setattr(skills_mod, "get_skill_manager", lambda: mgr)

    result = CliRunner().invoke(skills_mod.skills, ["validate", "known-skill"])
    assert result.exit_code == 0
    mgr.validate_skill.assert_called_once_with("known-skill")
