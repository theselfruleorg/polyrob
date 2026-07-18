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
        """
        from tools.code_exec import code_exec_docker_persistent_enabled

        sid = None
        if code_exec_docker_persistent_enabled():
            sid = getattr(execution_context, "session_id", None) or None

        if sid:
            key = (sid, bool(dev_mode))
            cached = self._persistent_backends.get(key)
            if cached is not None:
                return cached
            async with self._persistent_lock:
                cached = self._persistent_backends.get(key)  # re-check: lost the race?
                if cached is None:
                    if dev_mode:
                        cached = resolve_backend(session_id=sid, dev_mode=True)
                    else:
                        cached = resolve_backend(session_id=sid)
                    await cached.setup()
                    self._persistent_backends[key] = cached
                return cached

        if self._backend is None:
            backend = resolve_backend()
            await backend.setup()
            self._backend = backend
        return self._backend

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

    @staticmethod
    def _dev_mode_allowed(execution_context) -> bool:
        """True iff this call is entitled to sandbox-dev mode (WS-1).

        Rides the single posture predicate (posture >= 1 AND owner tenant AND
        not leaf/sub-agent AND not a forged turn). Fail-closed on any fault.
        """
        try:
            from core.config_policy import compute_posture_allows
            return bool(compute_posture_allows(execution_context, 1))
        except Exception:
            return False

    @BaseTool.action(
        "Execute python or bash code in a local subprocess (timeout + output cap + env allowlist)",
        param_model=RunCodeParams,
    )
    async def run_code(self, params: RunCodeParams, execution_context=None):
        """Execute ``params.code`` via the configured backend; return an ActionResult."""
        from tools.code_exec.sandbox_guard import code_exec_execution_blocked_reason
        from tools.controller.types import ActionResult
        blocked = code_exec_execution_blocked_reason()
        if blocked:
            return ActionResult(error=blocked)

        dev_mode = self._dev_mode_allowed(execution_context)
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
            result = await backend.run(req)
            return self._to_action_result(result)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"run_code failed: {e}")
            return ActionResult(error=f"run_code failed: {e}")
