"""The `self_env` self-maintenance tool (WS-5, posture 2). NO ``from __future__
import annotations`` — the action closures' Pydantic param models are introspected
by the Registry (the agent-upgrades-wave4 / GLM param_model landmine)."""
import logging
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.controller.types import ActionResult

# Conservative pip requirement-spec shape (name, extras, version pins). NOT a shell
# escape hatch — argv-list exec + this validation reject metacharacters early.
_PKG_SPEC_RE = re.compile(r"^[A-Za-z0-9._+\-\[\],]+(?:[=<>!~]=?[A-Za-z0-9._*+!,<>=]*)?$")
_READ_CAP = 100_000  # bytes returned by read_source
_SUBPROC_TIMEOUT = 300.0


class InstallDepParams(BaseModel):
    package: str = Field(..., description="A single pip requirement spec, e.g. 'flask==3.0.0'.")


class ReadSourceParams(BaseModel):
    path: str = Field(..., description="Path (relative to the install tree) to read.")


class PatchSourceParams(BaseModel):
    path: str = Field(..., description="Path (relative to the install tree) to edit.")
    old_string: str = Field(..., description="Exact unique string to replace.")
    new_string: str = Field(..., description="Replacement string.")


class RestartParams(BaseModel):
    pass


class GitPullParams(BaseModel):
    pass


class SelfEnvTool(BaseTool):
    """Narrow, approval-gated self-maintenance verbs (posture 2)."""

    def __init__(self, name: str = "self_env", config=None, container=None):
        super().__init__(name=name, config=config, container=container)
        self._install_root_override = None

    # --- gate + audit --------------------------------------------------------

    @staticmethod
    def _allowed(execution_context) -> bool:
        try:
            from agents.task.constants import compute_posture_allows
            return bool(compute_posture_allows(execution_context, 2))
        except Exception:
            return False

    def _deny(self) -> ActionResult:
        return ActionResult(error=(
            "self_env requires the self-maintain compute posture "
            "(AGENT_COMPUTE_POSTURE>=2) and an owner-steered, non-delegated turn; "
            "each verb is also approval-gated."
        ))

    def _emit(self, *, action: str, item_id: str = "", ok: bool = True,
              execution_context=None, **attrs) -> None:
        """Emit a self_modification audit event (fail-open)."""
        try:
            from agents.task.telemetry.self_events import emit_self_modification
            emit_self_modification(
                kind="self_env", action=action, item_id=item_id,
                user_id=str(getattr(execution_context, "user_id", "") or ""),
                session_id=str(getattr(execution_context, "session_id", "") or ""),
                source="self_env", ok=ok, **attrs)
        except Exception:
            pass

    # --- install-tree confinement --------------------------------------------

    def _install_root(self) -> Path:
        """Resolved install-tree root. ``POLYROB_INSTALL_TREE`` else the repo root
        (two levels up from this file). Realpath'd so confinement is symlink-safe."""
        if self._install_root_override:
            return Path(self._install_root_override).resolve()
        env = os.getenv("POLYROB_INSTALL_TREE")
        if env:
            return Path(env).resolve()
        return Path(__file__).resolve().parents[2]

    def _confine(self, rel_path: str) -> Tuple[Optional[Path], Optional[str]]:
        """Resolve ``rel_path`` inside the install tree. Returns (target, None) on
        success or (None, error) — realpath-confined AND env/config hard-denied."""
        root = self._install_root()
        target = (root / rel_path).resolve()
        from core.path_safety import is_within_root
        if not is_within_root(target, root):
            return None, f"path escapes the install tree: {rel_path}"
        # The `.git` dir is out of bounds: a patched `.git/config` (fsmonitor/hooksPath/
        # ext:: origin) executes code at self_env_git_pull time, sidestepping the git
        # RCE guard and the restart approval.
        if ".git" in target.parts:
            return None, f"refusing to touch the .git directory: {rel_path}"
        # WS-7 hard-deny: never let self_env touch the frozen security flags / secrets.
        # Use the BROAD secret classifier (is_secret_path) so in-tree app DBs
        # (data/**/bot.db, *.sqlite — may hold tokens/wallet material/PII) and named
        # credential files are denied too, not just the narrow name-glob set.
        from agents.task.agent.core.secret_guard import (
            is_credential_file, is_protected_config_path, is_secret_path,
        )
        if (is_credential_file(target) or is_protected_config_path(target)
                or is_secret_path(target, root=root)):
            return None, f"refusing to touch a credential/secret/config file: {rel_path}"
        return target, None

    # --- subprocess seam (injectable for tests) ------------------------------

    async def _run_subprocess(self, argv: List[str]) -> Tuple[int, str, str]:
        """Run ``argv`` (no shell) off the loop thread; return (rc, stdout, stderr)."""
        import asyncio

        def _sync():
            import subprocess
            env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL") if k in os.environ}
            env.setdefault("GIT_TERMINAL_PROMPT", "0")
            env["GIT_ALLOW_PROTOCOL"] = "file:git:http:https:ssh"  # no ext::/fd:: RCE
            try:
                proc = subprocess.run(argv, cwd=str(self._install_root()), env=env,
                                      capture_output=True, timeout=_SUBPROC_TIMEOUT)
                return (proc.returncode,
                        (proc.stdout or b"").decode("utf-8", "replace"),
                        (proc.stderr or b"").decode("utf-8", "replace"))
            except subprocess.TimeoutExpired:
                return 124, "", f"timed out after {_SUBPROC_TIMEOUT:.0f}s"
            except FileNotFoundError as e:
                return 127, "", f"executable not found: {e}"

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _sync)

    # --- verbs ---------------------------------------------------------------

    @BaseTool.action(
        "Install a pip dependency into the agent's own venv (single pinned spec)",
        param_model=InstallDepParams,
    )
    async def self_env_install_dep(self, params: InstallDepParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        pkg = (params.package or "").strip()
        # Reject a leading '-' BEFORE the shape check: '-' is inside _PKG_SPEC_RE's
        # char class (for names like 'foo-bar'), so a spec starting with '-' (e.g.
        # '-rreq.txt', '-e.', '--index-url') would otherwise pass and pip would parse
        # it as a FLAG — requirements-file install / editable / index override, an
        # escape from the single-pinned-package intent. Mirrors the git-tool guard.
        if not pkg or pkg.startswith("-") or not _PKG_SPEC_RE.fullmatch(pkg):
            return ActionResult(error=f"invalid package spec (a package name, not a pip flag): {params.package!r}")
        rc, out, err = await self._run_subprocess(
            [sys.executable, "-m", "pip", "install", "--no-input", pkg])
        ok = rc == 0
        self._emit(action="install_dep", item_id=pkg, ok=ok, execution_context=execution_context)
        if not ok:
            return ActionResult(error=f"pip install failed (exit {rc}):\n{(err or out)[-1500:]}")
        return ActionResult(extracted_content=f"installed {pkg}\n{out[-1000:]}", include_in_memory=True)

    @BaseTool.action(
        "Read a source file from the agent's install tree (confined; secrets denied)",
        param_model=ReadSourceParams,
    )
    async def self_env_read_source(self, params: ReadSourceParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        target, err = self._confine(params.path)
        if err:
            # a probe to read a secret / escape the tree is an auditable event
            self._emit(action="read_source", item_id=params.path, ok=False,
                       execution_context=execution_context, reason="confine_denied")
            return ActionResult(error=err)
        try:
            if not target.is_file():
                return ActionResult(error=f"file not found: {params.path}")
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ActionResult(error=f"read failed: {e}")
        if len(text) > _READ_CAP:
            text = text[:_READ_CAP] + f"\n...[truncated {len(text) - _READ_CAP} chars]"
        self._emit(action="read_source", item_id=params.path, ok=True,
                   execution_context=execution_context)
        return ActionResult(extracted_content=text)

    @BaseTool.action(
        "Edit a source file in the agent's install tree (unique str replace; secrets denied)",
        param_model=PatchSourceParams,
    )
    async def self_env_patch_source(self, params: PatchSourceParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        target, err = self._confine(params.path)
        if err:
            self._emit(action="patch_source", item_id=params.path, ok=False,
                       execution_context=execution_context, reason="confine_denied")
            return ActionResult(error=err)
        try:
            if not target.is_file():
                return ActionResult(error=f"file not found: {params.path}")
            content = target.read_text(encoding="utf-8")
            count = content.count(params.old_string)
            if count == 0:
                return ActionResult(error=f"old_string not found in {params.path}")
            if count > 1:
                return ActionResult(error=f"old_string is not unique in {params.path} ({count} matches)")
            target.write_text(content.replace(params.old_string, params.new_string), encoding="utf-8")
        except Exception as e:
            self._emit(action="patch_source", item_id=params.path, ok=False,
                       execution_context=execution_context)
            return ActionResult(error=f"patch failed: {e}")
        self._emit(action="patch_source", item_id=params.path, ok=True,
                   execution_context=execution_context)
        return ActionResult(extracted_content=f"patched {params.path}", include_in_memory=True)

    @BaseTool.action(
        "Fast-forward-only git pull on the agent's install tree (ext:: transport rejected)",
        param_model=GitPullParams,
    )
    async def self_env_git_pull(self, params: GitPullParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        # ff-only: never a merge commit the agent didn't author; GIT_ALLOW_PROTOCOL in
        # _run_subprocess rejects the ext::/fd:: transport RCE even from a poisoned config.
        rc, out, err = await self._run_subprocess(
            ["git", "-C", str(self._install_root()), "pull", "--ff-only"])
        ok = rc == 0
        self._emit(action="git_pull", item_id="", ok=ok, execution_context=execution_context)
        if not ok:
            return ActionResult(error=f"git pull failed (exit {rc}):\n{(err or out)[-1500:]}")
        return ActionResult(extracted_content=f"git pull:\n{out[-1500:]}", include_in_memory=True)

    def _schedule_restart(self) -> None:
        """Request a supervised respawn shortly AFTER this turn returns (so the reply
        is delivered), then exit with a code the supervisor restarts on. Overridable
        in tests."""
        import asyncio

        async def _later():
            await asyncio.sleep(2.0)
            os._exit(42)  # supervisor (systemd Restart=) respawns the fresh code

        try:
            asyncio.get_event_loop().create_task(_later())
        except Exception:
            logging.getLogger(__name__).error("could not schedule restart", exc_info=True)

    @BaseTool.action(
        "Request a supervised restart of the agent service (no-op unless supervised)",
        param_model=RestartParams,
    )
    async def self_env_restart_service(self, params: RestartParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        from core.env import bool_env
        if not bool_env("POLYROB_SUPERVISED", False):
            return ActionResult(error=(
                "restart refused: this process is not supervised (set POLYROB_SUPERVISED=1 "
                "only when a supervisor like systemd Restart= will respawn it). Exiting an "
                "unsupervised agent would kill it permanently."
            ))
        self._emit(action="restart_service", item_id="", ok=True, execution_context=execution_context)
        self._schedule_restart()
        return ActionResult(
            extracted_content="Restart requested — the supervisor will respawn the service shortly.",
            include_in_memory=True)
