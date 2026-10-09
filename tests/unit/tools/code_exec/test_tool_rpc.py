"""073 W9 — `run_code(tools=True)`: a python script calls allowlisted agent tools
over a per-run Unix-socket RPC, and every call re-enters the REAL Controller
dispatch path (multi_act), so pre-tool-call hooks still decide."""
import logging
import os
import sys
import types

import pytest
from pydantic import BaseModel, Field

from tools.code_exec import tool_rpc
from tools.code_exec.tool import CodeExecutionTool, RunCodeParams

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX sockets")


class _FetchParams(BaseModel):
    url: str = Field(..., description="url")


def _make_controller(tmp_path):
    import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator cycle
    from tools.controller.service import Controller

    orch = types.SimpleNamespace(session_id="s1", user_id="u1", workspace_dir=str(tmp_path))
    container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    return Controller(container=container, orchestrator=orch)


def _register_fake_fetch(controller, calls):
    """A fake action under the REAL registered name of the web_fetch verb."""
    @controller.registry.action("fake fetch", param_model=_FetchParams)
    async def web_fetch_fetch_url(params: _FetchParams, execution_context=None):
        from tools.controller.types import ActionResult
        calls.append((params.url, getattr(execution_context, "metadata", {}).get("via")))
        return ActionResult(extracted_content=f"PAGE<{params.url}>", include_in_memory=True)


def _ctx():
    from tools.controller.execution_context import ActionExecutionContext
    return ActionExecutionContext(role="orchestrator", session_id="s1", user_id="u1",
                                  metadata={"turn_kind": None})


def _tool(tmp_path, controller, *, tainted=False):
    t = object.__new__(CodeExecutionTool)
    t.logger = logging.getLogger("codeexec-rpc-test")
    t._backend = None
    t._persistent_backends = {}
    t._tool_rpc_orchestrator_resolver = lambda sid: types.SimpleNamespace(
        controller=controller, _correspondent_tainted=tainted)
    t._resolve_workdir = lambda ctx: str(tmp_path)
    t._dev_mode_allowed = lambda ctx: True
    return t


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.setenv("CODE_EXEC_TOOL_CALLS", "true")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "local_subprocess")
    monkeypatch.delenv("CODE_EXEC_MAX_TIMEOUT_SEC", raising=False)
    monkeypatch.setattr("tools.code_exec.tool.CodeExecutionTool._dev_mode_allowed",
                        staticmethod(lambda ctx: True))


async def _run(t, code):
    return await t.run_code(RunCodeParams(language="python", code=code, tools=True),
                            execution_context=_ctx())


@pytest.mark.asyncio
async def test_script_calls_an_allowlisted_action_through_the_controller(tmp_path, armed):
    c = _make_controller(tmp_path)
    calls = []
    _register_fake_fetch(c, calls)
    res = await _run(_tool(tmp_path, c), (
        "from polyrob_tools import web_fetch\n"
        "print('GOT', web_fetch('https://example.com/a'))\n"
        "print('KW', web_fetch(url='https://example.com/b'))\n"))
    assert not res.error, res.error
    assert "GOT PAGE<https://example.com/a>" in res.extracted_content
    assert "KW PAGE<https://example.com/b>" in res.extracted_content
    assert "[tool calls: 2/50]" in res.extracted_content
    # the action ran with a clone of the run's context, marked as a code call
    assert calls == [("https://example.com/a", "code_execution.tools"),
                     ("https://example.com/b", "code_execution.tools")]


@pytest.mark.asyncio
async def test_a_pre_hook_denial_raises_tool_error_in_the_script(tmp_path, armed):
    c = _make_controller(tmp_path)
    calls = []
    _register_fake_fetch(c, calls)
    c.register_pre_tool_call_hook(
        lambda name, params, ctx: "owner said no" if name == "web_fetch_fetch_url" else None)
    res = await _run(_tool(tmp_path, c), (
        "import polyrob_tools\n"
        "try:\n"
        "    polyrob_tools.web_fetch('https://example.com')\n"
        "except polyrob_tools.ToolError as e:\n"
        "    print('DENIED:', e)\n"))
    assert not res.error, res.error
    assert "DENIED:" in res.extracted_content and "owner said no" in res.extracted_content
    assert calls == []  # the hook vetoed it before the action ran


@pytest.mark.asyncio
async def test_a_disallowed_tool_is_refused(tmp_path, armed):
    c = _make_controller(tmp_path)
    res = await _run(_tool(tmp_path, c), (
        "import polyrob_tools\n"
        "for name in ('send_message', 'delegate_task'):\n"
        "    try:\n"
        "        polyrob_tools.call(name, text='hi')\n"
        "    except polyrob_tools.ToolError as e:\n"
        "        print('REFUSED', name, e)\n"))
    assert not res.error, res.error
    assert "REFUSED send_message" in res.extracted_content
    assert "REFUSED delegate_task" in res.extracted_content


@pytest.mark.asyncio
async def test_an_unloaded_allowlisted_tool_is_an_exception_not_a_crash(tmp_path, armed):
    c = _make_controller(tmp_path)  # no web_fetch registered
    res = await _run(_tool(tmp_path, c), "from polyrob_tools import web_fetch\nweb_fetch('x')\n")
    assert res.error and "ToolError" in res.error and "not loaded" in res.error


@pytest.mark.asyncio
async def test_the_call_cap_is_enforced(tmp_path, armed):
    c = _make_controller(tmp_path)
    calls = []
    _register_fake_fetch(c, calls)
    res = await _run(_tool(tmp_path, c), (
        "import polyrob_tools\n"
        "n = 0\n"
        "try:\n"
        "    for i in range(60):\n"
        "        polyrob_tools.web_fetch('https://example.com/%d' % i)\n"
        "        n += 1\n"
        "except polyrob_tools.ToolError as e:\n"
        "    print('STOPPED AFTER', n, '-', e)\n"))
    assert not res.error, res.error
    assert "STOPPED AFTER 50 - tool RPC call cap reached" in res.extracted_content
    assert len(calls) == 50


@pytest.mark.asyncio
async def test_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.delenv("CODE_EXEC_TOOL_CALLS", raising=False)
    c = _make_controller(tmp_path)
    res = await _run(_tool(tmp_path, c), "print(1)")
    assert res.error and "CODE_EXEC_TOOL_CALLS" in res.error


@pytest.mark.asyncio
async def test_refused_without_the_compute_posture(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.setenv("CODE_EXEC_TOOL_CALLS", "true")
    monkeypatch.setattr("tools.code_exec.tool.CodeExecutionTool._dev_mode_allowed",
                        staticmethod(lambda ctx: False))
    c = _make_controller(tmp_path)
    t = _tool(tmp_path, c)
    del t._dev_mode_allowed
    res = await _run(t, "print(1)")
    assert res.error and "compute posture" in res.error


@pytest.mark.asyncio
async def test_refused_for_a_leaf_or_sub_agent(tmp_path, armed):
    from tools.controller.execution_context import ActionExecutionContext
    c = _make_controller(tmp_path)
    t = _tool(tmp_path, c)
    for ctx in (ActionExecutionContext(role="leaf", session_id="s1"),
                ActionExecutionContext(role="orchestrator", is_sub_agent=True, session_id="s1")):
        res = await t.run_code(RunCodeParams(language="python", code="print(1)", tools=True),
                               execution_context=ctx)
        assert res.error and "sub-agent" in res.error


@pytest.mark.asyncio
async def test_refused_on_a_correspondent_tainted_turn(tmp_path, armed):
    c = _make_controller(tmp_path)
    res = await _run(_tool(tmp_path, c, tainted=True), "print(1)")
    assert res.error and "untrusted" in res.error


@pytest.mark.asyncio
async def test_bash_is_refused(tmp_path, armed):
    c = _make_controller(tmp_path)
    res = await _tool(tmp_path, c).run_code(
        RunCodeParams(language="bash", code="echo hi", tools=True), execution_context=_ctx())
    assert res.error and "python" in res.error


# -- the policy, derived from core/tool_capabilities ---------------------------

def test_every_allowlisted_tool_passes_the_capability_policy():
    from core.tool_capabilities import TOOL_CAPABILITIES
    owners = {"web_fetch_fetch_url": "web_fetch", "knowledge_kb_search": "knowledge",
              "knowledge_kb_list": "knowledge", "filesystem_read_file": "filesystem",
              "filesystem_write_file": "filesystem", "filesystem_append_file": "filesystem",
              "filesystem_list_directory": "filesystem", "shell_run": "shell",
              "memory_search": None, "session_search": None}
    for action, tid in owners.items():
        if tid is not None:
            assert tid in TOOL_CAPABILITIES
        assert tool_rpc.policy_refusal(action, tid) is None, action


@pytest.mark.parametrize("tid,needle", [
    ("defi_trade", "money"), ("x402_pay", "money"), ("email", "shared identity"),
    ("mcp", "MCP"), ("code_execution", "delegate-blocked"), ("browser", "write effect"),
    ("not_a_tool", "not classified"),
])
def test_a_dangerous_owner_is_refused_even_for_an_allowlisted_name(tid, needle):
    why = tool_rpc.policy_refusal("web_fetch_fetch_url", tid)
    assert why and needle in why


def test_never_actions_and_unlisted_actions_are_refused():
    for a in ("send_message", "delegate_task", "tool_call", "code_execution_run_code"):
        assert "never" in tool_rpc.policy_refusal(a, None)
    assert "allowlist" in tool_rpc.policy_refusal("filesystem_delete_file", "filesystem")


@pytest.mark.asyncio
async def test_server_refuses_a_bad_nonce_and_forces_a_foreground_shell():
    seen = []

    async def dispatch(action, args):
        seen.append((action, dict(args)))
        return types.SimpleNamespace(error=None, extracted_content="ok")

    srv = tool_rpc.ToolRpcServer(socket_path="/unused", nonce="n1", dispatch=dispatch,
                                 resolve=lambda a: (True, "shell"), shell_ceiling=600)
    srv._deadline = float("inf")
    bad = await srv._serve({"nonce": "nope", "tool": "shell", "args": {"command": "ls"}})
    assert not bad["ok"] and "nonce" in bad["error"]
    bg = await srv._serve({"nonce": "n1", "tool": "shell",
                           "args": {"command": "sleep 1", "background": True}})
    assert not bg["ok"] and "foreground" in bg["error"]
    ok = await srv._serve({"nonce": "n1", "tool": "shell",
                           "args": {"command": "ls", "timeout": 10_000}})
    assert ok == {"ok": True, "result": "ok"}
    action, args = seen[-1]
    assert action == "shell_run" and args["background"] is False and args["timeout"] <= 600


@pytest.mark.asyncio
async def test_server_truncates_a_long_result():
    async def dispatch(action, args):
        return types.SimpleNamespace(error=None, extracted_content="x" * 100)

    srv = tool_rpc.ToolRpcServer(socket_path="/unused", nonce="n", dispatch=dispatch,
                                 resolve=lambda a: (True, "web_fetch"), max_result_chars=10)
    srv._deadline = float("inf")
    r = await srv._serve({"nonce": "n", "tool": "web_fetch", "args": {"url": "u"}})
    assert r["ok"] and r["result"].startswith("x" * 10) and "truncated 90 chars" in r["result"]


@pytest.mark.asyncio
async def test_server_refuses_after_the_wall_clock_cap():
    async def dispatch(action, args):  # pragma: no cover - never reached
        raise AssertionError("dispatched past the deadline")

    srv = tool_rpc.ToolRpcServer(socket_path="/unused", nonce="n", dispatch=dispatch,
                                 resolve=lambda a: (True, "web_fetch"))
    srv._deadline = 0.0
    r = await srv._serve({"nonce": "n", "tool": "web_fetch", "args": {"url": "u"}})
    assert not r["ok"] and "wall-clock" in r["error"]


# -- backends --------------------------------------------------------------------

def test_ssh_and_unknown_backends_get_an_honest_error():
    plan, why = tool_rpc.plan_socket(types.SimpleNamespace(name="ssh"))
    assert plan is None and "not supported" in why and "ssh" in why


def test_docker_ephemeral_binds_the_socket_dir_read_only():
    from tools.code_exec.backends.docker import DockerBackend
    from tools.code_exec.result import ExecutionRequest
    b = DockerBackend()
    plan, why = tool_rpc.plan_socket(b)
    try:
        assert why is None and plan.mount_dir and plan.script_path.startswith("/polyrob_rpc/")
        argv = b._build_run_argv(
            ExecutionRequest(language="python", code="print(1)", tool_rpc_dir=plan.mount_dir),
            "/tmp/ws")
        i = argv.index("--mount", argv.index(b.user))
        assert argv[i + 1] == f"type=bind,src={plan.mount_dir},dst=/polyrob_rpc,readonly"
        plain = b._build_run_argv(ExecutionRequest(language="python", code="print(1)"), "/tmp/ws")
        assert not any("/polyrob_rpc" in a for a in plain)
    finally:
        tool_rpc.cleanup_plan(plan)
    assert not os.path.exists(plan.host_dir)


def test_docker_persistent_needs_the_setup_mount(monkeypatch):
    from tools.code_exec.backends.docker import DockerBackend
    b = DockerBackend(session_id="s1")
    plan, why = tool_rpc.plan_socket(b)
    assert plan is None and "CODE_EXEC_TOOL_CALLS" in why
    monkeypatch.setenv("CODE_EXEC_TOOL_CALLS", "true")
    flags = b._tool_rpc_setup_flags()
    try:
        assert flags and flags[0] == "--mount" and "dst=/polyrob_rpc,readonly" in flags[1]
        plan, why = tool_rpc.plan_socket(b)
        assert why is None and plan.owns_dir is False
        assert plan.host_path.startswith(b.tool_rpc_host_dir)
        assert plan.script_path.startswith("/polyrob_rpc/")
    finally:
        import shutil
        shutil.rmtree(b.tool_rpc_host_dir, ignore_errors=True)


def test_docker_persistent_setup_unchanged_when_the_flag_is_off(monkeypatch):
    from tools.code_exec.backends.docker import DockerBackend
    monkeypatch.delenv("CODE_EXEC_TOOL_CALLS", raising=False)
    b = DockerBackend(session_id="s1")
    assert b._tool_rpc_setup_flags() == [] and b.tool_rpc_host_dir is None


def test_the_prelude_is_one_line_and_builds_the_module():
    wrapped = tool_rpc.wrap_script("x = 1\n", "/tmp/s.sock", "nonce")
    assert wrapped.count("\n") == 2 and wrapped.endswith("x = 1\n")
    ns = {}
    exec(compile(wrapped, "<t>", "exec"), ns)
    mod = sys.modules.pop("polyrob_tools")
    assert {"web_fetch", "read_file", "shell", "ToolError"} <= set(dir(mod))


def test_prelude_goes_after_future_imports():
    """073 W9 follow-up: a script that starts with `from __future__` still runs."""
    import subprocess, sys
    from tools.code_exec.tool_rpc import wrap_script
    code = '"""doc"""\nfrom __future__ import annotations\nimport polyrob_tools\nprint("ok", hasattr(polyrob_tools, "call"))\n'
    wrapped = wrap_script(code, "/nonexistent.sock", "n")
    assert wrapped.index("from __future__") < wrapped.index("polyrob_tools'")
    out = subprocess.run([sys.executable, "-I", "-c", wrapped], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok True"
