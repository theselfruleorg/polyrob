"""``code_execution`` tool (Item 3 — WS-C3).

One action, ``run_code(language, code, stdin?, timeout?)``, that resolves the
configured ``ExecutionBackend`` and returns an ``ActionResult``. Output is already
capped by the backend; failures (non-zero exit / timeout) surface as ``error``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.code_exec import resolve_backend
from tools.code_exec.result import ExecutionRequest

# Conservative pip requirement-spec shape (name, extras, version pins) — NOT a shell
# escape hatch. Everything is additionally shlex-quoted; this just rejects obvious
# junk early with a clear message instead of a confusing in-sandbox pip error.
_PKG_SPEC_RE = re.compile(r"^[A-Za-z0-9._+\-\[\],]+(?:[=<>!~]=?[A-Za-z0-9._*+!,<>=]*)?$")
_MAX_PACKAGES = 20


class RunCodeParams(BaseModel):
    """Parameters for the ``run_code`` action (plain Pydantic — native-schema safe)."""

    language: str = Field(..., description="Language to run: 'python' or 'bash'")
    code: str = Field(..., description="Source code to execute")
    stdin: Optional[str] = Field(None, description="Optional stdin fed to the program")
    timeout: Optional[float] = Field(
        None, description="Wall-clock seconds (clamped to CODE_EXEC_MAX_TIMEOUT_SEC)"
    )
    env: Optional[Dict[str, str]] = Field(
        None,
        description="Extra environment variables for the sandboxed process "
                    "(secret-named keys are stripped)",
    )
    packages: Optional[List[str]] = Field(
        None,
        description="pip packages to install into the sandbox's /install dir before "
                    "running (e.g. ['flask==3.0.0', 'pytest']). Requires the "
                    "sandbox-dev compute posture and sandbox network.",
    )
    tools: bool = Field(
        False,
        description="python only: let the script call agent tools via "
                    "`from polyrob_tools import web_fetch, web_search, read_file, "
                    "write_file, append_file, list_directory, kb_search, kb_list, "
                    "memory_search, session_search, shell`. Each call runs through the "
                    "normal tool gates; a refusal raises polyrob_tools.ToolError. Max 50 "
                    "calls / 300 s per run. Needs CODE_EXEC_TOOL_CALLS and the sandbox-dev "
                    "compute posture.",
    )
    persist: bool = Field(
        False,
        description="python only: run in this session's persistent kernel — variables, "
                    "imports and functions from earlier persist=True calls stay defined, "
                    "and a last expression is echoed. A timeout restarts the kernel "
                    "(state lost). Not with tools=True or stdin.",
    )
    reset_kernel: bool = Field(
        False, description="with persist=True: start a fresh kernel first (drop all state).",
    )


class CodeExecutionTool(BaseTool):
    """Runs code through a pluggable execution backend (default: local subprocess)."""

    def __init__(self, name: str = "code_execution", config=None, container=None):
        super().__init__(name=name, config=config, container=container)
        self._backend = None
        # P1-B F7b: PERSISTENT backends are session-scoped — never share ONE
        # persistent DockerBackend (bound to one session's container) across
        # sessions the way `self._backend` caches the ephemeral one. Keyed by
        # session_id; the lock guards create-once-under-races per tool instance
        # (setup() is rare/first-call-only, so coarse-grained is fine).
        self._persistent_backends: dict = {}
        self._persistent_lock = asyncio.Lock()

    async def _get_backend(self, execution_context=None, dev_mode: bool = False):
        """Resolve the backend to run on.

        PERSISTENT (opt-in, P1-B F7b): when ``CODE_EXEC_DOCKER_PERSISTENT`` is on
        AND ``execution_context`` carries a truthy ``session_id``, resolve via
        ``resolve_backend(session_id=sid)`` and cache it PER SESSION — the
        container is created once (one ``setup()`` call) and reused for every
        later ``run_code`` call in that session.

        EPHEMERAL (default, byte-for-byte unchanged): flag off, or no
        session_id — one process-wide, session-less backend cached on
        ``self._backend``, exactly as before this change.

        ``dev_mode`` (WS-1): a posture-entitled call resolves/caches a DEV
        persistent backend (writable ``/install`` mounted at setup). The cache is
        keyed ``(sid, dev_mode)`` so a dev and a non-dev container for the same
        session never share mounts; non-dev keeps the legacy
        ``resolve_backend(session_id=sid)`` call shape byte-identically.

        Shared logic lives in ``tools.code_exec.backend_cache`` (dedup with
        ``CodingTool._get_code_exec_backend``). ``resolve_backend`` is passed as
        this module's attribute so tests patching
        ``tools.code_exec.tool.resolve_backend`` keep working.
        """
        from tools.code_exec.backend_cache import resolve_cached_backend

        return await resolve_cached_backend(self, execution_context, dev_mode, resolve_backend)

    def _resolve_workdir(self, execution_context) -> Optional[str]:
        """Run inside the session workspace when a session is resolvable, else tempdir."""
        try:
            sid = getattr(execution_context, "session_id", None) or getattr(self, "session_id", None)
            if sid:
                from agents.task.path import pm
                return str(pm().get_workspace_dir(sid))
        except Exception:
            pass
        return None

    @staticmethod
    def _to_action_result(result):
        from tools.controller.types import ActionResult

        parts = []
        if result.stdout:
            parts.append(result.stdout)
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr}")
        if result.truncated:
            parts.append("[output truncated]")
        content = "\n".join(parts) if parts else "(no output)"

        if result.timed_out:
            return ActionResult(error=f"code execution timed out after {result.duration_sec:.1f}s\n{content}")
        if result.exit_code not in (0, None):
            return ActionResult(error=f"code exited with status {result.exit_code}\n{content}")
        return ActionResult(extracted_content=content)

    async def _run_persistent(self, backend, params, workdir, execution_context):
        """073: one cell in the session's persistent kernel (tools/code_exec/kernel.py)."""
        from tools.code_exec.kernel import KernelUnavailable, run_cell
        from tools.code_exec.limits import dev_exec_max_timeout_sec
        from tools.controller.types import ActionResult
        sid = getattr(execution_context, "session_id", None) or "kernel"
        timeout = max(1.0, min(float(params.timeout or 60.0), dev_exec_max_timeout_sec()))
        try:
            cell = await run_cell(sid, backend, params.code, timeout=timeout,
                                  workdir=workdir, reset=params.reset_kernel)
        except KernelUnavailable as e:
            return ActionResult(error=str(e))
        parts = []
        if cell.restarted:
            parts.append("[new kernel]")
        if cell.stdout:
            parts.append(cell.stdout.rstrip("\n"))
        if cell.stderr:
            parts.append(f"[stderr]\n{cell.stderr.rstrip()}")
        if cell.note:
            parts.append(f"[{cell.note}]")
        content = "\n".join(parts) if parts else "(no output)"
        if not cell.ok:
            return ActionResult(error=content)
        return ActionResult(extracted_content=content)

    @staticmethod
    def _dev_mode_allowed(execution_context) -> bool:
        """True iff this call is entitled to sandbox-dev mode (WS-1).

        Rides the single posture predicate (posture >= 1 AND owner tenant AND
        not leaf/sub-agent AND not a forged turn). Fail-closed on any fault.
        """
        from core.config_policy import compute_posture_allows_safe
        return compute_posture_allows_safe(execution_context, 1)

    # -- 073 W9: code that calls tools (tools/code_exec/tool_rpc.py) ------------

    def _tool_rpc_orchestrator(self, execution_context):
        """The live orchestrator of the calling session (its controller dispatches
        every tool call). ``_tool_rpc_orchestrator_resolver`` is a test seam."""
        from tools.ship_common import resolve_orchestrator
        sid = getattr(execution_context, "session_id", None) or ""
        if not sid:
            return None
        return resolve_orchestrator(lambda: self.container, sid,
                                    getattr(self, "_tool_rpc_orchestrator_resolver", None))

    def _tool_rpc_refusal(self, params, execution_context, dev_mode: bool):
        """Why ``tools=True`` may not run on this call, or None. Fail-closed."""
        from tools.code_exec.tool_rpc import tool_rpc_enabled
        if not tool_rpc_enabled():
            return ("tools=True is off on this instance (CODE_EXEC_TOOL_CALLS). Run the "
                    "code without tools, or call the tools directly.")
        if (params.language or "").lower() not in ("python", "python3", "py"):
            return "tools=True needs language='python'."
        if not dev_mode:
            return ("tools=True requires the sandbox-dev compute posture "
                    "(AGENT_COMPUTE_POSTURE>=1) and an owner-steered, non-delegated turn.")
        if getattr(execution_context, "is_sub_agent", False) or \
                getattr(execution_context, "role", "leaf") != "orchestrator":
            return "tools=True is not available to a sub-agent or a leaf."
        orch = self._tool_rpc_orchestrator(execution_context)
        controller = getattr(orch, "controller", None) if orch is not None else None
        if controller is None or getattr(controller, "registry", None) is None:
            return "tools=True could not reach this session's tool controller."
        try:
            tainted = bool(getattr(orch, "_correspondent_tainted", False))
            if not tainted:
                from core.security.refusal_taint import is_tainted
                tainted = is_tainted(getattr(execution_context, "session_id", None))
        except Exception:
            tainted = True  # can't prove clean -> deny
        if tainted:
            return ("tools=True is refused on this turn: the latest input is untrusted "
                    "correspondent data, or this run is refusal-tainted.")
        return None

    async def _run_with_tools(self, backend, req, execution_context):
        """Run *req* with a per-run tool-RPC server bound to the session's controller."""
        from tools.code_exec import tool_rpc
        from tools.code_exec.limits import dev_exec_max_timeout_sec
        from tools.controller.tool_call_bridge import perform_tool_call
        from tools.controller.types import ActionResult

        controller = self._tool_rpc_orchestrator(execution_context).controller
        plan, why = tool_rpc.plan_socket(backend)
        if plan is None:
            return ActionResult(error=why)
        meta = dict(getattr(execution_context, "metadata", None) or {})
        meta["via"] = "code_execution.tools"

        async def _dispatch(action: str, args: dict):
            # THE one dispatch path: multi_act runs every pre-tool-call hook.
            ctx = execution_context.clone(metadata=dict(meta))
            return await perform_tool_call(controller, action, args, execution_context=ctx)

        def _resolve(action: str):
            a = controller.registry.get_action(action)
            return (a is not None, getattr(a, "tool", None) if a is not None else None)

        nonce = tool_rpc.new_nonce()
        wall = tool_rpc.WALL_CLOCK_SEC
        req.code = tool_rpc.wrap_script(req.code, plan.script_path, nonce, wall_sec=wall)
        req.timeout = min(float(req.timeout), wall) if req.timeout else wall
        req.ceiling = wall
        req.tool_rpc_dir = plan.mount_dir
        server = tool_rpc.ToolRpcServer(
            socket_path=plan.host_path, nonce=nonce, dispatch=_dispatch, resolve=_resolve,
            owner=getattr(backend, "user", None), wall_sec=wall,
            shell_ceiling=dev_exec_max_timeout_sec())
        try:
            async with server:
                result = await backend.run(req)
        finally:
            tool_rpc.cleanup_plan(plan)
        out = self._to_action_result(result)
        note = f"\n[tool calls: {server.stats.calls}/{server.max_calls}"
        note += f", refused: {server.stats.refused}]" if server.stats.refused else "]"
        if out.error:
            out.error += note
        else:
            out.extracted_content = (out.extracted_content or "") + note
        return out

    @BaseTool.action(
        "Execute python or bash code on the configured execution backend (CODE_EXEC_BACKEND: "
        "local subprocess by default, or docker/ssh), with a timeout, an output cap and an "
        "env allowlist. On a server it is refused unless the backend is a sandbox.",
        param_model=RunCodeParams,
    )
    async def run_code(self, params: RunCodeParams, execution_context=None):
        """Execute ``params.code`` via the configured backend; return an ActionResult."""
        from tools.code_exec.sandbox_guard import code_exec_execution_blocked_reason
        from tools.controller.types import ActionResult
        from tools.code_exec.sandbox_guard import local_host_exec_refusal
        blocked = (code_exec_execution_blocked_reason()
                   or local_host_exec_refusal(execution_context))
        if blocked:
            return ActionResult(error=blocked)

        # Every language that is not python reaches a shell (`bash -c`) on some
        # backend ("shell" is an alias there), so the guard classifies all of them.
        if (params.language or "").strip().lower() not in {"python", "python3", "py"}:
            from core.security.command_guard import classify
            verdict = classify(params.code)
            if verdict.is_floor:
                return ActionResult(error=f"refused by command guard: {verdict.reason}")

        dev_mode = self._dev_mode_allowed(execution_context)
        if params.reset_kernel and not params.persist:
            return ActionResult(error="reset_kernel needs persist=True")
        if params.persist:
            if (params.language or "").strip().lower() != "python":
                return ActionResult(error="persist=True runs python only")
            if params.tools or params.stdin:
                return ActionResult(error="persist=True cannot be combined with tools=True or stdin")
        if params.tools:
            refusal = self._tool_rpc_refusal(params, execution_context, dev_mode)
            if refusal:
                return ActionResult(error=refusal)
        packages = [str(p).strip() for p in (params.packages or []) if str(p).strip()]
        if packages:
            if not dev_mode:
                return ActionResult(error=(
                    "packages requires the sandbox-dev compute posture "
                    "(AGENT_COMPUTE_POSTURE>=1) and an owner-steered session. "
                    "Write code that needs no extra dependencies, or ask the "
                    "operator to raise the posture."
                ))
            if len(packages) > _MAX_PACKAGES:
                return ActionResult(error=f"too many packages (max {_MAX_PACKAGES})")
            # A leading '-' is a pip FLAG (e.g. '-rreq.txt' requirements-file install,
            # '-e' editable, '--index-url' override), NOT a package — reject it even
            # though shlex.quote wouldn't (a dash isn't a shell metachar, so pip still
            # parses it as a flag and escapes the per-package-spec intent).
            bad = [p for p in packages if p.startswith("-") or not _PKG_SPEC_RE.fullmatch(p)]
            if bad:
                return ActionResult(error=f"invalid package spec(s) (package names, not pip flags): {bad!r}")
        try:
            backend = await self._get_backend(execution_context, dev_mode=dev_mode)
            workdir = self._resolve_workdir(execution_context)

            if packages:
                # 014 B2: gate on the network the install will ACTUALLY get, not
                # the raw env — a posture-1 persistent dev container auto-bridges
                # when CODE_EXEC_NETWORK is unset (docker.py::_resolve_setup_network).
                # Backends without the probe fall back to the env policy unchanged.
                eff = None
                probe = getattr(backend, "effective_setup_network", None)
                if callable(probe):
                    try:
                        eff = str(probe()).lower()
                    except Exception:
                        eff = None
                if eff is None:
                    eff = (os.getenv("CODE_EXEC_NETWORK", "none") or "none").lower()
                if eff in ("none", ""):
                    return ActionResult(error=(
                        "packages needs sandbox network egress, but the effective "
                        "sandbox network is 'none'. The operator must set "
                        "CODE_EXEC_NETWORK=egress (or run at AGENT_COMPUTE_POSTURE>=1 "
                        "with the persistent dev container, which networks itself)."
                    ))

            if packages:
                quoted = " ".join(shlex.quote(p) for p in packages)
                install_req = ExecutionRequest(
                    language="bash",
                    code=f"python -m pip install --no-input --target=/install {quoted}",
                    timeout=None,  # backend clamps to CODE_EXEC_MAX_TIMEOUT_SEC
                    workdir=workdir,
                    dev_mode=True,
                )
                inst = await backend.run(install_req)
                if inst.timed_out or inst.exit_code not in (0, None):
                    tail = (inst.stderr or inst.stdout or "")[-1500:]
                    return ActionResult(error=(
                        f"package install failed (exit {inst.exit_code}"
                        f"{', timed out' if inst.timed_out else ''}):\n{tail}"
                    ))

            req = ExecutionRequest(
                language=params.language,
                code=params.code,
                stdin=params.stdin,
                timeout=params.timeout,
                workdir=workdir,
                env=dict(params.env or {}),
                dev_mode=dev_mode,
            )
            if params.persist:
                return await self._run_persistent(backend, params, workdir, execution_context)
            if params.tools:
                return await self._run_with_tools(backend, req, execution_context)
            result = await backend.run(req)
            return self._to_action_result(result)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"run_code failed: {e}")
            return ActionResult(error=f"run_code failed: {e}")
