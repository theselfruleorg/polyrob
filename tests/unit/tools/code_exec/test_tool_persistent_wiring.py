"""P1-B F7b — wire CODE_EXEC_DOCKER_PERSISTENT into CodeExecutionTool.run_code.

Closes the documented wiring gap in `tools/code_exec/__init__.py::
code_exec_docker_persistent_enabled`'s docstring: when the flag is on AND the
action's `execution_context` carries a truthy `session_id`, `_get_backend` must
resolve a PERSISTENT backend via `resolve_backend(session_id=sid)`, cache it
PER SESSION (never leak session A's container into session B's calls), and
call `setup()` exactly once — reusing the same backend object on later calls
for the same session. Flag off, or no session_id, must stay byte-identical to
the pre-existing ephemeral, session-less caching (`resolve_backend()` with no
kwargs, cached once on `self._backend`).

These tests fully replace `resolve_backend` with a spy, so they need no Docker
daemon and no real container.
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from tools.code_exec.tool import CodeExecutionTool, RunCodeParams


class _SpyBackend:
    """A minimal fake ExecutionBackend: records setup()/run() calls."""

    def __init__(self):
        self.setup_calls = 0
        self.run_calls = []

    async def setup(self):
        self.setup_calls += 1

    async def run(self, request):
        self.run_calls.append(request)
        from tools.code_exec.result import ExecutionResult
        return ExecutionResult(stdout="ok", exit_code=0, backend="spy")

    async def teardown(self):
        pass


def _tool():
    t = object.__new__(CodeExecutionTool)
    t.logger = logging.getLogger("code-exec-persist-test")
    t._backend = None
    t._persistent_backends = {}
    t._persistent_lock = asyncio.Lock()
    return t


def _install_spy_resolve_backend(monkeypatch):
    calls = []
    backend = _SpyBackend()

    def _spy(*, session_id=None):
        calls.append(session_id)
        return backend

    monkeypatch.setattr("tools.code_exec.tool.resolve_backend", _spy)
    return calls, backend


# --------------------------------------------------------------------------
# Flag ON + truthy session_id -> PERSISTENT, created once, reused
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_code_uses_persistent_backend_when_flag_on_and_session_id(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    monkeypatch.setenv("POLYROB_LOCAL", "true")  # bypass the sandbox gate, unrelated to this test
    calls, backend = _install_spy_resolve_backend(monkeypatch)

    tool = _tool()
    ctx = SimpleNamespace(session_id="s1")

    r1 = await tool.run_code(RunCodeParams(language="python", code="print(1)"), execution_context=ctx)
    r2 = await tool.run_code(RunCodeParams(language="python", code="print(2)"), execution_context=ctx)

    assert calls == ["s1"]  # resolve_backend(session_id="s1") called exactly ONCE
    assert backend.setup_calls == 1  # setup() called exactly ONCE
    assert len(backend.run_calls) == 2  # reused for both calls
    assert getattr(r1, "error", None) in (None, "")
    assert getattr(r2, "error", None) in (None, "")


@pytest.mark.asyncio
async def test_run_code_caches_persistent_backend_per_session_id(monkeypatch):
    """Two DIFFERENT sessions must never share a persistent backend/container —
    the cache key is session_id, not a single instance-level slot."""
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    calls = []
    backends_by_session = {}

    def _spy(*, session_id=None):
        calls.append(session_id)
        b = _SpyBackend()
        backends_by_session[session_id] = b
        return b

    monkeypatch.setattr("tools.code_exec.tool.resolve_backend", _spy)

    tool = _tool()
    ctx_a = SimpleNamespace(session_id="session-a")
    ctx_b = SimpleNamespace(session_id="session-b")

    await tool.run_code(RunCodeParams(language="python", code="print(1)"), execution_context=ctx_a)
    await tool.run_code(RunCodeParams(language="python", code="print(1)"), execution_context=ctx_b)
    await tool.run_code(RunCodeParams(language="python", code="print(2)"), execution_context=ctx_a)

    assert sorted(calls) == ["session-a", "session-b"]  # resolved once per session
    assert backends_by_session["session-a"] is not backends_by_session["session-b"]
    assert len(backends_by_session["session-a"].run_calls) == 2
    assert len(backends_by_session["session-b"].run_calls) == 1


# --------------------------------------------------------------------------
# Flag OFF -> ephemeral, session-less, existing behavior preserved
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_code_stays_ephemeral_when_flag_off(monkeypatch):
    monkeypatch.delenv("CODE_EXEC_DOCKER_PERSISTENT", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    calls, backend = _install_spy_resolve_backend(monkeypatch)

    tool = _tool()
    ctx = SimpleNamespace(session_id="s1")

    await tool.run_code(RunCodeParams(language="python", code="print(1)"), execution_context=ctx)
    await tool.run_code(RunCodeParams(language="python", code="print(2)"), execution_context=ctx)

    assert calls == [None]  # resolve_backend() called WITHOUT session_id, once (cached on self._backend)
    assert backend.setup_calls == 1
    assert len(backend.run_calls) == 2


@pytest.mark.asyncio
async def test_run_code_stays_ephemeral_when_flag_on_but_no_session_id(monkeypatch):
    """Flag on alone is not enough — a falsy/missing session_id must still
    resolve the ephemeral, session-less backend (both conditions are AND'd)."""
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    calls, backend = _install_spy_resolve_backend(monkeypatch)

    tool = _tool()

    await tool.run_code(RunCodeParams(language="python", code="print(1)"), execution_context=None)
    await tool.run_code(
        RunCodeParams(language="python", code="print(2)"),
        execution_context=SimpleNamespace(session_id=""),
    )

    assert calls == [None]  # both calls fall through to the ephemeral path, cached once


@pytest.mark.asyncio
async def test_run_code_no_execution_context_kwarg_still_works(monkeypatch):
    """The overwhelming majority of existing call sites never pass
    execution_context at all — must be completely unaffected."""
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    calls, backend = _install_spy_resolve_backend(monkeypatch)

    tool = _tool()
    result = await tool.run_code(RunCodeParams(language="python", code="print(1)"))

    assert calls == [None]
    assert getattr(result, "error", None) in (None, "")
