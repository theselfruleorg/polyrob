"""``coding`` tool (H10-B / H9 SPEC §4): str_replace / grep / run_tests.

A minimal single-user coding surface. Edits are confined to the session workspace
(or CWD) root; ``str_replace`` is exact + unique-or-fail; ``grep`` is the pure
gitignore-aware search; ``run_tests`` routes through the existing code_exec backend
(inherits its pgroup-kill timeout + output cap + secret-free env).

LANDMINE: NO ``from __future__ import annotations`` in this module — it holds the
action closures whose param models the registry resolves.
"""
import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.coding.edit import apply_str_replace, apply_patch, EditError
from tools.coding.search import search_files


class CodingError(Exception):
    """Raised for confinement / IO errors at the tool boundary."""


class StrReplaceParams(BaseModel):
    file_path: str = Field(..., description="Path to the file to edit (relative to the workspace, or absolute within it)")
    old_string: str = Field(..., description="Exact text to replace; must be unique unless replace_all=true")
    new_string: str = Field(..., description="Replacement text")
    replace_all: bool = Field(False, description="Replace every occurrence instead of failing on ambiguity")


class ApplyPatchParams(BaseModel):
    file_path: str = Field(..., description="Path to the file to patch (relative to the workspace, or absolute within it)")
    patch: str = Field(..., description="Unified diff for this one file (@@ hunks). Context/removed lines must match exactly.")


class GrepParams(BaseModel):
    pattern: str = Field(..., description="Regex to search for")
    path: Optional[str] = Field(None, description="Subdir (relative to workspace) to search; defaults to the whole workspace")
    glob: Optional[str] = Field(None, description="Filename glob filter, e.g. '*.py'")
    output_mode: str = Field("content", description="'content' (path:line:text) or 'files' (matching paths only)")


class RunTestsParams(BaseModel):
    command: Optional[str] = Field(None, description="Test command to run; defaults to 'pytest -q'")
    timeout: Optional[float] = Field(None, description="Wall-clock seconds (clamped by the code_exec backend)")


class CreateFileParams(BaseModel):
    file_path: str = Field(..., description="Path of the new file (relative to the workspace)")
    content: str = Field("", description="Initial file content")
    overwrite: bool = Field(False, description="Allow overwriting an existing file")


class MoveFileParams(BaseModel):
    src_path: str = Field(..., description="Existing path to move (relative to the workspace)")
    dest_path: str = Field(..., description="Destination path (relative to the workspace)")
    overwrite: bool = Field(False, description="Allow overwriting an existing destination")


class DeleteFileParams(BaseModel):
    file_path: str = Field(..., description="Path to delete (relative to the workspace)")


class CodingTool(BaseTool):
    """Edit/search/test the workspace. Gated by CODING_TOOLS_ENABLED."""

    def __init__(self, name: str = "coding", config=None, container=None):
        super().__init__(name=name, config=config, container=container)
        self._backend = None
        # P1-B F7b: mirrors CodeExecutionTool._get_backend's session-scoped cache
        # (see _get_code_exec_backend below) — a PERSISTENT backend is bound to
        # ONE session's container and must never be shared across sessions the
        # way `self._backend` shares the ephemeral one.
        self._persistent_backends: dict = {}
        self._persistent_lock = asyncio.Lock()
        self._root_override = None

    # --- root + confinement --------------------------------------------------

    def _resolve_root(self, execution_context=None) -> str:
        if self._root_override:
            return os.path.abspath(self._root_override)
        try:
            sid = getattr(execution_context, "session_id", None) or getattr(self, "session_id", None)
            if sid:
                from agents.task.path import pm
                return str(pm().get_workspace_dir(sid))
        except Exception:
            pass
        return os.getcwd()

    def _confine(self, file_path: str, root: str) -> str:
        target = os.path.abspath(os.path.join(root, file_path))
        # Confine on realpath (both sides) so an in-root symlink can't redirect a
        # write outside; operate on the lexical target so a legitimate in-root
        # symlink keeps its semantics. Reuses the shared helper (single source).
        from core.path_safety import is_within_root
        if not is_within_root(target, root):
            raise CodingError(f"path escapes the workspace root: {file_path}")
        # Secret-content guard: refuse credential-shaped targets (.env*, *.pem,
        # config/.env.*, …) even when in-root. Under POLYROB_LOCAL the workspace is
        # the project cwd, so confinement alone can't stop a coding action from
        # reading/rewriting a config/.env.production that lives in the project.
        from agents.task.agent.core.secret_guard import is_credential_file
        if is_credential_file(Path(target)):
            raise CodingError(f"refusing to touch a credential/secret file: {file_path}")
        return target

    # --- code_exec backend resolution (P1-B F7b) ------------------------------

    async def _get_code_exec_backend(self, execution_context=None, dev_mode: bool = False):
        """Resolve the code_exec backend for ``run_tests``.

        PERSISTENT (opt-in, P1-B F7b): when ``CODE_EXEC_DOCKER_PERSISTENT`` is on
        AND ``execution_context`` carries a truthy ``session_id``, resolve via
        ``resolve_backend(session_id=sid)`` and cache it PER SESSION — the
        container is created once (one ``setup()`` call) and reused for every
        later ``run_tests`` call in that session. Mirrors
        ``tools.code_exec.tool.CodeExecutionTool._get_backend``.

        EPHEMERAL (default, byte-for-byte unchanged): flag off, or no
        session_id — one process-wide, session-less backend cached on
        ``self._backend``, exactly as before this change.

        ``dev_mode`` (WS-1): a posture-entitled call caches a DEV persistent
        backend under ``(sid, True)`` (writable ``/install`` mounted at setup) so
        a pytest installed by ``run_code(packages=[...])`` is importable here.
        Non-dev keeps the legacy ``resolve_backend(session_id=sid)`` call shape.
        """
        from tools.code_exec import resolve_backend, code_exec_docker_persistent_enabled

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

    @staticmethod
    def _ok(content):
        from tools.controller.types import ActionResult
        return ActionResult(extracted_content=content)

    @staticmethod
    def _err(msg):
        from tools.controller.types import ActionResult
        return ActionResult(error=msg)

    # --- actions -------------------------------------------------------------

    @BaseTool.action(
        "Replace an exact, unique string in a file (fails loudly on 0 or >1 matches unless replace_all)",
        param_model=StrReplaceParams,
    )
    async def str_replace(self, params: StrReplaceParams, execution_context=None):
        try:
            root = self._resolve_root(execution_context)
            target = self._confine(params.file_path, root)
            if not os.path.isfile(target):
                return self._err(f"file not found: {params.file_path}")
            with open(target, "r", encoding="utf-8") as f:
                content = f.read()
            try:
                updated = apply_str_replace(
                    content, params.old_string, params.new_string, params.replace_all
                )
            except EditError as e:
                return self._err(str(e))
            with open(target, "w", encoding="utf-8") as f:
                f.write(updated)
            n = content.count(params.old_string) if params.replace_all else 1
            return self._ok(f"Edited {params.file_path} ({n} replacement{'s' if n != 1 else ''}).")
        except CodingError as e:
            return self._err(str(e))
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"str_replace failed: {e}")
            return self._err(f"str_replace failed: {e}")

    @BaseTool.action(
        "Apply a unified-diff patch to one file (reject-on-context-mismatch; fails loudly if context doesn't match)",
        param_model=ApplyPatchParams,
    )
    async def apply_patch(self, params: ApplyPatchParams, execution_context=None):
        try:
            root = self._resolve_root(execution_context)
            target = self._confine(params.file_path, root)
            if not os.path.isfile(target):
                return self._err(f"file not found: {params.file_path}")
            with open(target, "r", encoding="utf-8") as f:
                content = f.read()
            try:
                updated = apply_patch(content, params.patch)
            except EditError as e:
                return self._err(str(e))
            with open(target, "w", encoding="utf-8") as f:
                f.write(updated)
            return self._ok(f"Patched {params.file_path}.")
        except CodingError as e:
            return self._err(str(e))
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"apply_patch failed: {e}")
            return self._err(f"apply_patch failed: {e}")

    @BaseTool.action(
        "Search files for a regex (gitignore-aware); output_mode 'content' or 'files'",
        param_model=GrepParams,
    )
    async def grep(self, params: GrepParams, execution_context=None):
        try:
            root = self._resolve_root(execution_context)
            search_root = self._confine(params.path, root) if params.path else root
            hits = search_files(
                search_root, params.pattern, glob=params.glob, output_mode=params.output_mode,
            )
            if not hits:
                return self._ok("(no matches)")
            if params.output_mode == "files":
                rels = [os.path.relpath(p, root) for p in hits]
                return self._ok("\n".join(rels))
            lines = [f"{os.path.relpath(h.path, root)}:{h.line_no}:{h.line}" for h in hits]
            return self._ok("\n".join(lines))
        except CodingError as e:
            return self._err(str(e))
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"grep failed: {e}")
            return self._err(f"grep failed: {e}")

    @BaseTool.action(
        "Run the test suite (default 'pytest -q') in the workspace via the code_exec backend "
        "(sandbox-gated on servers; local convenience under POLYROB_LOCAL)",
        param_model=RunTestsParams,
    )
    async def run_tests(self, params: RunTestsParams, execution_context=None):
        from tools.code_exec.sandbox_guard import code_exec_execution_blocked_reason
        blocked = code_exec_execution_blocked_reason()
        if blocked:
            return self._err(blocked)
        try:
            from tools.code_exec.result import ExecutionRequest

            root = self._resolve_root(execution_context)
            command = params.command or "pytest -q"
            # WS-1: an entitled session runs importable-mode (PYTHONPATH=/install)
            # so packages installed via run_code(packages=[...]) are visible here.
            try:
                from agents.task.constants import compute_posture_allows
                dev_mode = bool(compute_posture_allows(execution_context, 1))
            except Exception:
                dev_mode = False
            backend = await self._get_code_exec_backend(execution_context, dev_mode=dev_mode)
            req = ExecutionRequest(
                language="bash", code=command, timeout=params.timeout, workdir=root,
                dev_mode=dev_mode,
            )
            result = await backend.run(req)
            parts = []
            if result.stdout:
                parts.append(result.stdout)
            if result.stderr:
                parts.append(f"[stderr]\n{result.stderr}")
            if result.truncated:
                parts.append("[output truncated]")
            content = "\n".join(parts) if parts else "(no output)"
            if result.timed_out:
                return self._err(f"tests timed out after {result.duration_sec:.1f}s\n{content}")
            if result.exit_code not in (0, None):
                return self._err(f"tests failed (exit {result.exit_code})\n{content}")
            return self._ok(content)
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"run_tests failed: {e}")
            return self._err(f"run_tests failed: {e}")

    @BaseTool.action(
        "Create a new file in the workspace (fails if it exists unless overwrite=true)",
        param_model=CreateFileParams,
    )
    async def create_file(self, params: CreateFileParams, execution_context=None):
        try:
            root = self._resolve_root(execution_context)
            target = self._confine(params.file_path, root)
            if os.path.isdir(target):
                return self._err(f"path is a directory: {params.file_path}")
            if os.path.exists(target) and not params.overwrite:
                return self._err(f"file already exists: {params.file_path} (set overwrite=true)")
            os.makedirs(os.path.dirname(target) or root, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(params.content or "")
            return self._ok(f"Created {params.file_path} ({len(params.content or '')} bytes).")
        except CodingError as e:
            return self._err(str(e))
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"create_file failed: {e}")
            return self._err(f"create_file failed: {e}")

    @BaseTool.action("Move/rename a file within the workspace", param_model=MoveFileParams)
    async def move_file(self, params: MoveFileParams, execution_context=None):
        try:
            root = self._resolve_root(execution_context)
            src = self._confine(params.src_path, root)
            dest = self._confine(params.dest_path, root)
            if not os.path.exists(src):
                return self._err(f"source not found: {params.src_path}")
            if os.path.exists(dest) and not params.overwrite:
                return self._err(f"destination exists: {params.dest_path} (set overwrite=true)")
            os.makedirs(os.path.dirname(dest) or root, exist_ok=True)
            import shutil as _shutil
            _shutil.move(src, dest)
            return self._ok(f"Moved {params.src_path} -> {params.dest_path}.")
        except CodingError as e:
            return self._err(str(e))
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"move_file failed: {e}")
            return self._err(f"move_file failed: {e}")

    @BaseTool.action("Delete a file within the workspace", param_model=DeleteFileParams)
    async def delete_file(self, params: DeleteFileParams, execution_context=None):
        try:
            root = self._resolve_root(execution_context)
            target = self._confine(params.file_path, root)
            if not os.path.exists(target):
                return self._err(f"file not found: {params.file_path}")
            if os.path.isdir(target):
                return self._err(f"refusing to delete a directory: {params.file_path}")
            os.remove(target)
            return self._ok(f"Deleted {params.file_path}.")
        except CodingError as e:
            return self._err(str(e))
        except Exception as e:
            getattr(self, "logger", logging.getLogger(__name__)).error(f"delete_file failed: {e}")
            return self._err(f"delete_file failed: {e}")
