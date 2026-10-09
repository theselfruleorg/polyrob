"""Shared base for the cloud / CLI sandbox backends the W7 packs ship (073 W7).

``packs/modal``, ``packs/daytona``, ``packs/vercel_sandbox`` and
``packs/singularity`` each subclass :class:`RemoteSandboxBackend` and implement
three primitives — ``_provision`` (create or resume the sandbox), ``_exec`` (run
an argv inside it) and ``_release`` (stop / snapshot / delete). The base owns the
contract every caller relies on:

- the ``ExecutionBackend`` lifecycle (``setup``/``run``/``exec_detached``/``teardown``),
  session-scoped (``session_id`` set: ONE sandbox per session, reused per call) or
  ephemeral (``session_id=None``: a sandbox per ``run``, released after it);
- ``capabilities["sandbox"] = True`` (the sandbox guard admits it on a server);
- the timeout cap (``exec_timeout_cap``, the dev ceiling), the in-sandbox
  ``timeout --signal=KILL`` wrapper and the 124/137 -> ``timed_out`` mapping;
- the env rule: only ``request.env`` minus every secret-NAMED var reaches the
  sandbox. The provider's own credentials (``MODAL_TOKEN_*``, ``DAYTONA_API_KEY``,
  ``VERCEL_TOKEN`` …) authenticate the SDK client in THIS process and are never
  forwarded;
- the network rule, mirroring the docker backend: ``CODE_EXEC_NETWORK`` wins; unset
  -> a dev sandbox is networked (installs), a confined one is not;
- a small local state file per (provider, session) for resume handles
  (``<data_home>/sandbox_state/<provider>/<sid>.json``) — the pack decides what to
  keep (a Modal snapshot image id, a Daytona sandbox id).

SDKs are optional extras: :func:`require_sdk` imports lazily and raises an
honest ``ExecutionBackendError`` naming ``pip install 'polyrob[<extra>]'``.

No ``@BaseTool.action`` closures — ``from __future__`` is safe.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from tools.code_exec.backend import ExecutionBackend, ExecutionBackendError
from tools.code_exec.env_policy import SECRET_PAT
from tools.code_exec.limits import exec_timeout_cap, max_output_bytes, max_timeout_sec
from tools.code_exec.result import ExecutionRequest, ExecutionResult

logger = logging.getLogger(__name__)

_PY = ("python", "python3", "py")
_SH = ("bash", "sh", "shell")
_TIMEOUT_EXIT_CODES = frozenset({124, 137})
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def require_sdk(modules: Union[str, Sequence[str]], *, extra: str, what: str) -> Any:
    """Import the first importable of ``modules`` or raise the honest remedy."""
    names = (modules,) if isinstance(modules, str) else tuple(modules)
    last: Optional[BaseException] = None
    for name in names:
        try:
            return importlib.import_module(name)
        except ImportError as exc:
            last = exc
    raise ExecutionBackendError(
        f"{what} needs its SDK ({' or '.join(names)}), which is not installed: "
        f"pip install 'polyrob[{extra}]'" + (f" ({last})" if last else ""))


def env_value(*names: str) -> str:
    """The first non-empty env value of ``names`` ("" when none)."""
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def session_slug(session_id: Optional[str]) -> str:
    """A short, provider-safe name component for a session."""
    sid = session_id or "ephemeral"
    clean = re.sub(r"[^a-z0-9-]", "-", sid.lower()).strip("-")[:24] or "s"
    return f"{clean}-{hashlib.sha256(sid.encode()).hexdigest()[:8]}"


class RemoteSandboxBackend(ExecutionBackend):
    """Base of the W7 pack backends (see the module docstring)."""

    name = "remote_sandbox"
    #: ``capabilities["isolation"]`` — the provider's boundary (``"gvisor"``, ``"vm"``, …).
    isolation = "remote"
    #: The pack extra that carries the SDK (``""`` = none, a CLI backend).
    extra = ""
    #: The far-side working directory the shell starts in.
    workdir = "/workspace"
    #: Whether the provider can deny the sandbox network (else ``network`` stays True).
    can_block_network = True

    def __init__(self, *, session_id: Optional[str] = None, dev_mode: bool = False) -> None:
        self._session_id = session_id
        self._dev_mode = bool(dev_mode)
        self.max_timeout = max_timeout_sec(dev_mode)
        self.max_output = max_output_bytes()
        self._handle: Any = None
        self._lock = asyncio.Lock()

    # --- the three primitives a pack implements -----------------------------------
    async def _provision(self) -> Any:  # pragma: no cover - abstract
        """Create (or resume) the sandbox; return its handle."""
        raise NotImplementedError

    async def _exec(self, argv: List[str], *, timeout: float, stdin: Optional[str],
                    env: Dict[str, str]) -> Tuple[int, str, str]:  # pragma: no cover
        """Run ``argv`` inside the sandbox; ``(rc, stdout, stderr)``."""
        raise NotImplementedError

    async def _release(self, handle: Any) -> None:  # pragma: no cover - abstract
        """Stop / snapshot / delete the sandbox."""
        raise NotImplementedError

    # --- shared contract ---------------------------------------------------------
    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def persistent(self) -> bool:
        return self._session_id is not None

    def network_enabled(self) -> bool:
        if not self.can_block_network:
            return True
        raw = os.getenv("CODE_EXEC_NETWORK")
        if raw is None:
            return self._dev_mode
        return raw.strip().lower() not in ("none", "")

    @property
    def capabilities(self) -> Dict[str, object]:
        return {"network": self.network_enabled(), "isolation": self.isolation,
                "sandbox": True}

    async def setup(self) -> None:
        if self._handle is not None:
            return
        async with self._lock:
            if self._handle is None:
                self._handle = await self._provision()

    async def teardown(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            await self._release(handle)
        except Exception:
            logger.warning("%s backend: release failed", self.name, exc_info=True)

    def _clamp_timeout(self, t, ceiling=None) -> float:
        cap = exec_timeout_cap(self.max_timeout, ceiling)
        if t is None:
            return cap
        return max(1.0, min(float(t), cap))

    def _cap_text(self, text: Optional[str]) -> Tuple[str, bool]:
        text = text or ""
        if len(text) > self.max_output:
            return text[: self.max_output] + f"\n...[truncated {len(text) - self.max_output} chars]", True
        return text, False

    @staticmethod
    def safe_env(env: Optional[Dict[str, str]]) -> Dict[str, str]:
        """``request.env`` minus every secret-named or non-identifier key."""
        out: Dict[str, str] = {}
        for k, v in (env or {}).items():
            if SECRET_PAT.search(k) or not _ENV_KEY_RE.match(k):
                continue
            out[k] = str(v)
        return out

    def command_argv(self, request: ExecutionRequest, timeout: float) -> List[str]:
        """PURE: the in-sandbox argv (``timeout --signal=KILL`` + interpreter)."""
        lang = (request.language or "").lower()
        argv = ["timeout", "--signal=KILL", str(int(max(1, round(timeout))))]
        if lang in _PY:
            argv += ["python3", "-c", request.code]
        else:
            argv += ["bash", "-c", request.code]
        return argv

    async def run(self, request: ExecutionRequest) -> ExecutionResult:
        lang = (request.language or "").lower()
        if lang not in _PY and lang not in _SH:
            return ExecutionResult(stderr=f"unsupported language '{request.language}' "
                                          "(use python|bash)", exit_code=2, backend=self.name)
        timeout = self._clamp_timeout(request.timeout, getattr(request, "ceiling", None))
        start = time.monotonic()
        try:
            await self.setup()
            # The in-sandbox `timeout` is the kill; this bound only stops a hung
            # provider call from holding the turn.
            rc, out, err = await asyncio.wait_for(
                self._exec(self.command_argv(request, timeout), timeout=timeout + 15,
                           stdin=request.stdin, env=self.safe_env(request.env)),
                timeout + 60)
            timed_out = rc in _TIMEOUT_EXIT_CODES
        except ExecutionBackendError as exc:
            return ExecutionResult(stderr=str(exc), exit_code=2, backend=self.name,
                                   duration_sec=time.monotonic() - start)
        except asyncio.TimeoutError:
            rc, out, err, timed_out = 124, "", f"{self.name}: timed out after {timeout:g}s", True
        except Exception as exc:  # noqa: BLE001 — a provider fault is this run's error
            logger.debug("%s backend: run failed", self.name, exc_info=True)
            return ExecutionResult(stderr=f"{self.name} backend error: {type(exc).__name__}: {exc}",
                                   exit_code=1, backend=self.name,
                                   duration_sec=time.monotonic() - start)
        finally:
            if not self.persistent:
                await self.teardown()
        out_text, t1 = self._cap_text(out)
        err_text, t2 = self._cap_text(err)
        return ExecutionResult(stdout=out_text, stderr=err_text, exit_code=rc,
                               timed_out=timed_out, truncated=t1 or t2,
                               duration_sec=time.monotonic() - start, backend=self.name)

    async def exec_detached(self, script: str) -> int:
        """PERSISTENT-only: run a launcher ``script`` (it backgrounds its own job
        with ``nohup setsid``, so a short foreground exec is enough)."""
        if not self.persistent:
            raise ExecutionBackendError(
                f"exec_detached requires a session-scoped {self.name} backend")
        await self.setup()
        rc, out, err = await self._exec(["bash", "-c", script], timeout=45, stdin=None, env={})
        if rc != 0:
            raise ExecutionBackendError(
                f"{self.name}: background launch failed (exit {rc}): {(err or out)[:300]}")
        return rc

    # --- the resume-handle store ---------------------------------------------------
    def state_path(self) -> Optional[Path]:
        if not self._session_id:
            return None
        try:
            from core.runtime_paths import effective_data_home
            base = Path(effective_data_home())
        except Exception:
            return None
        return base / "sandbox_state" / self.name / f"{session_slug(self._session_id)}.json"

    def load_state(self) -> Dict[str, Any]:
        path = self.state_path()
        if path is None or not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, data: Dict[str, Any]) -> None:
        path = self.state_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            logger.warning("%s backend: could not save resume state", self.name, exc_info=True)

    def clear_state(self) -> None:
        path = self.state_path()
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    @staticmethod
    async def in_thread(fn, *args, **kwargs):
        """Run a blocking SDK call off the event loop."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    @staticmethod
    def shell_join(argv: Sequence[str]) -> str:
        return " ".join(shlex.quote(a) for a in argv)


__all__ = ["RemoteSandboxBackend", "env_value", "require_sdk", "session_slug"]
