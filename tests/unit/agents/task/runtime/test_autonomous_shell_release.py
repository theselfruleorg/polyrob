"""2026-08-16: autonomous runs must release their persistent shell sandbox.

Session end runs only a PARTIAL cleanup (the orchestrator stays resident for
continuous chat), which skips the shell-container teardown, and reap_orphans
is cold-start-only by design — so every goal/cron run that used the shell tool
leaked one docker container until the next service restart (live 2026-08-16:
7 `polyrob-sbx-*` containers, 3–21h old, on an 8GB box).

run_task_to_outcome (the ONE helper goal dispatch + cron share) now releases
the session's pooled shell backend after the outcome is assembled — for
autonomous runs only; an interactive chat session legitimately keeps its
container between turns. The release routes through
SessionCleanupMixin.release_shell_sandbox, the owner of the one allowlisted
agents→tools.shell layering edge.
"""
import asyncio

from agents.task.runtime.run_as_session import run_task_to_outcome
from agents.task.session.cleanup import SessionCleanupMixin


class _FakeOrch:
    def __init__(self):
        self.released = 0

    async def release_shell_sandbox(self):
        self.released += 1


class _FakeAgent:
    def __init__(self, orch=None):
        self.orch = orch

    async def create_session(self, *, user_id, request, **kwargs):
        return {"id": "sess-shell-1"}

    async def run_session(self, user_id, session_id):
        return "Session completed successfully"

    def get_orchestrator(self, session_id):
        return self.orch


def test_autonomous_run_releases_shell_backend():
    orch = _FakeOrch()
    outcome = asyncio.run(run_task_to_outcome(
        _FakeAgent(orch), user_id="rob", request={}, autonomous=True))
    assert outcome.session_id == "sess-shell-1"
    assert orch.released == 1


def test_interactive_run_keeps_shell_backend():
    orch = _FakeOrch()
    outcome = asyncio.run(run_task_to_outcome(
        _FakeAgent(orch), user_id="rob", request={}, autonomous=False))
    assert outcome.session_id == "sess-shell-1"
    assert orch.released == 0, "a chat session keeps its container between turns"


def test_release_failure_never_breaks_the_run():
    class _BoomOrch:
        async def release_shell_sandbox(self):
            raise RuntimeError("docker down")

    outcome = asyncio.run(run_task_to_outcome(
        _FakeAgent(_BoomOrch()), user_id="rob", request={}, autonomous=True))
    assert outcome.session_id == "sess-shell-1"
    assert outcome.refusal is False


def test_missing_orchestrator_is_tolerated():
    outcome = asyncio.run(run_task_to_outcome(
        _FakeAgent(None), user_id="rob", request={}, autonomous=True))
    assert outcome.session_id == "sess-shell-1"


def test_mixin_release_calls_backend_pool_teardown(monkeypatch):
    """The mixin method itself must reach tools.shell.backend_pool with the
    session id (and swallow errors — it runs inside teardown paths)."""
    released = []

    async def _fake_teardown(sid):
        released.append(sid)

    monkeypatch.setattr(
        "tools.shell.backend_pool.teardown_session", _fake_teardown)

    orch = object.__new__(SessionCleanupMixin)
    orch.session_id = "sess-mixin-1"
    asyncio.run(orch.release_shell_sandbox())
    assert released == ["sess-mixin-1"]

    async def _boom(sid):
        raise RuntimeError("docker down")

    monkeypatch.setattr("tools.shell.backend_pool.teardown_session", _boom)
    asyncio.run(orch.release_shell_sandbox())  # must not raise
