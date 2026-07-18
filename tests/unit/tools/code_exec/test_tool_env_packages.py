"""WS-1 (computer-use parity): run_code gains `env` + `packages` params.

`env` plumbs caller env into the sandbox (ExecutionRequest.env — secret-scrubbed
downstream). `packages` declaratively pip-installs into /install BEFORE the code
runs — posture-gated (compute_posture_allows(ctx, 1)) and requiring sandbox
network; both preconditions fail with a CLEAR message, never silently.
"""
import pytest

import agents.task.constants as c
from tools.code_exec.tool import CodeExecutionTool, RunCodeParams
from tools.controller.execution_context import ActionExecutionContext


class _RecordingBackend:
    """Stands in for the resolved ExecutionBackend; records every request."""

    def __init__(self, exit_code=0):
        self.requests = []
        self.exit_code = exit_code

    async def run(self, request):
        from tools.code_exec.result import ExecutionResult
        self.requests.append(request)
        return ExecutionResult(stdout="ok", exit_code=self.exit_code, backend="fake")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "POLYROB_LOCAL", "CODE_EXEC_NETWORK",
              "POLYROB_OWNER_USER_ID", "CODE_EXEC_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    # neutralize the server sandbox guard so unit tests exercise the new logic only
    monkeypatch.setattr(
        "tools.code_exec.sandbox_guard.code_exec_execution_blocked_reason", lambda: None
    )
    c._refreeze_compute_posture_for_tests()
    yield
    # LIFO landmine (see d99b8bb3 / docs/ops/inbox.md 2026-07-14): this teardown
    # runs BEFORE monkeypatch reverts env, so delenv explicitly or the refreeze
    # re-snapshots a test's posture and leaks it module-globally.
    monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
    c._refreeze_compute_posture_for_tests()


def _tool(backend):
    import asyncio
    import logging
    t = object.__new__(CodeExecutionTool)  # BaseTool __init__ needs a config; skip it
    t.logger = logging.getLogger("code-exec-env-pkg-test")
    t._backend = None
    t._persistent_backends = {}
    t._persistent_lock = asyncio.Lock()
    async def _fake_get_backend(execution_context=None, dev_mode=False):
        return backend
    t._get_backend = _fake_get_backend
    return t


def _owner_ctx():
    # clean env: owner principal defaults to the instance id 'rob'
    return ActionExecutionContext(role="orchestrator", is_sub_agent=False,
                                  user_id="rob", session_id="s1",
                                  metadata={"turn_kind": None})


def _posture(monkeypatch, val):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", val)
    c._refreeze_compute_posture_for_tests()


@pytest.mark.asyncio
async def test_env_param_flows_to_execution_request():
    be = _RecordingBackend()
    t = _tool(be)
    res = await t.run_code(
        RunCodeParams(language="python", code="print(1)", env={"FOO": "bar"}),
        execution_context=_owner_ctx(),
    )
    assert not res.error
    assert be.requests[0].env == {"FOO": "bar"}


@pytest.mark.asyncio
async def test_packages_denied_below_posture_1(monkeypatch):
    be = _RecordingBackend()
    t = _tool(be)
    res = await t.run_code(
        RunCodeParams(language="python", code="import cowsay", packages=["cowsay"]),
        execution_context=_owner_ctx(),
    )
    assert res.error and "AGENT_COMPUTE_POSTURE" in res.error
    assert be.requests == []  # nothing ran


@pytest.mark.asyncio
async def test_packages_denied_for_non_owner_even_at_posture_1(monkeypatch):
    _posture(monkeypatch, "1")
    be = _RecordingBackend()
    t = _tool(be)
    ctx = _owner_ctx()
    ctx.user_id = "u_stranger"
    res = await t.run_code(
        RunCodeParams(language="python", code="x", packages=["cowsay"]),
        execution_context=ctx,
    )
    assert res.error and be.requests == []


@pytest.mark.asyncio
async def test_packages_need_sandbox_network(monkeypatch):
    _posture(monkeypatch, "1")  # network still 'none'
    be = _RecordingBackend()
    t = _tool(be)
    res = await t.run_code(
        RunCodeParams(language="python", code="x", packages=["cowsay"]),
        execution_context=_owner_ctx(),
    )
    assert res.error and "CODE_EXEC_NETWORK" in res.error
    assert be.requests == []


@pytest.mark.asyncio
async def test_packages_install_then_run(monkeypatch):
    _posture(monkeypatch, "1")
    monkeypatch.setenv("CODE_EXEC_NETWORK", "egress")
    be = _RecordingBackend()
    t = _tool(be)
    res = await t.run_code(
        RunCodeParams(language="python", code="import cowsay", packages=["cowsay"]),
        execution_context=_owner_ctx(),
    )
    assert not res.error
    assert len(be.requests) == 2
    install, code = be.requests
    assert install.language == "bash"
    assert "pip install" in install.code and "--target=/install" in install.code
    assert "cowsay" in install.code
    assert install.dev_mode is True
    assert code.code == "import cowsay"
    assert code.dev_mode is True  # entitled session runs importable-mode python


@pytest.mark.asyncio
async def test_install_failure_short_circuits(monkeypatch):
    _posture(monkeypatch, "1")
    monkeypatch.setenv("CODE_EXEC_NETWORK", "egress")
    be = _RecordingBackend(exit_code=1)
    t = _tool(be)
    res = await t.run_code(
        RunCodeParams(language="python", code="import cowsay", packages=["cowsay"]),
        execution_context=_owner_ctx(),
    )
    assert res.error and "install" in res.error.lower()
    assert len(be.requests) == 1  # code never ran


@pytest.mark.asyncio
async def test_malicious_package_name_rejected(monkeypatch):
    _posture(monkeypatch, "1")
    monkeypatch.setenv("CODE_EXEC_NETWORK", "egress")
    be = _RecordingBackend()
    t = _tool(be)
    res = await t.run_code(
        RunCodeParams(language="python", code="x", packages=["cowsay; rm -rf /"]),
        execution_context=_owner_ctx(),
    )
    assert res.error and "package" in res.error.lower()
    assert be.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("pkg", ["-rreq.txt", "-e.", "--index-url", "--pre"])
async def test_packages_reject_pip_flag_injection(monkeypatch, pkg):
    """A package starting with '-' is a pip FLAG (requirements-file/editable/index
    override), not a package — must be rejected even though shlex.quote would quote it,
    because pip still parses it as a flag."""
    _posture(monkeypatch, "1")
    monkeypatch.setenv("CODE_EXEC_NETWORK", "egress")
    be = _RecordingBackend()
    t = _tool(be)
    res = await t.run_code(
        RunCodeParams(language="python", code="x", packages=[pkg]),
        execution_context=_owner_ctx(),
    )
    assert res.error and "package" in res.error.lower(), f"{pkg!r} should be rejected"
    assert be.requests == []


@pytest.mark.asyncio
async def test_entitled_session_runs_dev_mode_even_without_packages(monkeypatch):
    _posture(monkeypatch, "1")
    be = _RecordingBackend()
    t = _tool(be)
    res = await t.run_code(
        RunCodeParams(language="python", code="print(1)"),
        execution_context=_owner_ctx(),
    )
    assert not res.error
    assert be.requests[0].dev_mode is True


@pytest.mark.asyncio
async def test_unentitled_session_stays_isolated():
    be = _RecordingBackend()
    t = _tool(be)
    res = await t.run_code(
        RunCodeParams(language="python", code="print(1)"),
        execution_context=_owner_ctx(),  # posture 0
    )
    assert not res.error
    assert be.requests[0].dev_mode is False


# --- 014 B2: the packages gate honors the backend's EFFECTIVE network ------------
# (a posture-1 persistent dev container auto-bridges when CODE_EXEC_NETWORK is
# unset — docker.py::_resolve_setup_network — so a raw-env check wrongly refused
# pip installs on a container that actually has egress).


class _BridgedBackend(_RecordingBackend):
    def effective_setup_network(self) -> str:
        return "bridge"


class _NoNetBackend(_RecordingBackend):
    def effective_setup_network(self) -> str:
        return "none"


@pytest.mark.asyncio
async def test_packages_allowed_when_backend_reports_bridge(monkeypatch):
    _posture(monkeypatch, "1")
    b = _BridgedBackend()
    t = _tool(b)
    res = await t.run_code(
        RunCodeParams(language="python", code="print(1)", packages=["requests"]),
        execution_context=_owner_ctx())
    assert not res.error
    assert any("pip install" in r.code for r in b.requests)  # install ran


@pytest.mark.asyncio
async def test_packages_refused_when_backend_reports_none(monkeypatch):
    _posture(monkeypatch, "1")
    t = _tool(_NoNetBackend())
    res = await t.run_code(
        RunCodeParams(language="python", code="print(1)", packages=["requests"]),
        execution_context=_owner_ctx())
    assert res.error and "network" in res.error.lower()


@pytest.mark.asyncio
async def test_packages_env_fallback_when_backend_has_no_probe(monkeypatch):
    # backend without effective_setup_network: raw-env behavior is unchanged
    _posture(monkeypatch, "1")
    monkeypatch.setenv("CODE_EXEC_NETWORK", "egress")
    b = _RecordingBackend()
    t = _tool(b)
    res = await t.run_code(
        RunCodeParams(language="python", code="print(1)", packages=["requests"]),
        execution_context=_owner_ctx())
    assert not res.error
