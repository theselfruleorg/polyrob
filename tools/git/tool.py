"""``git`` tool (P0-D): structured git over the confined workspace root.

Every command runs ``git -C <root> …`` in a worker thread with a wall-clock timeout;
the root is realpath-confined (a clone target and every staged path must stay inside
it). ``git_push`` is high-impact (approval-gated + leaf-blocked at the gate layer, Task 9).

LANDMINE: NO ``from __future__ import annotations`` — this module holds the action
closures whose param models the registry introspects.
"""
import logging
import os
from typing import List, Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool


class GitError(Exception):
    """Raised for confinement errors at the tool boundary."""


class GitStatusParams(BaseModel):
    pass


class GitDiffParams(BaseModel):
    path: Optional[str] = Field(None, description="Limit the diff to this path (relative to the workspace)")
    staged: bool = Field(False, description="Show staged (--cached) changes")


class GitLogParams(BaseModel):
    max_count: int = Field(20, description="How many commits to show (1-200)")


class GitBranchParams(BaseModel):
    name: Optional[str] = Field(None, description="Create this branch (omit to list branches)")


class GitCheckoutParams(BaseModel):
    ref: str = Field(..., description="Branch/commit to checkout")
    create: bool = Field(False, description="Create the branch (-b) before checking out")


class GitAddParams(BaseModel):
    paths: List[str] = Field(..., description="Paths to stage (relative to the workspace)")


class GitCommitParams(BaseModel):
    message: str = Field(..., description="Commit message")
    all: bool = Field(False, description="Stage all tracked modifications first (-a)")


class GitPullParams(BaseModel):
    remote: str = Field("origin", description="Remote name")
    branch: Optional[str] = Field(None, description="Branch to pull (default: current)")


class GitPushParams(BaseModel):
    remote: str = Field("origin", description="Remote name")
    branch: Optional[str] = Field(None, description="Branch to push (default: current)")
    set_upstream: bool = Field(False, description="Set upstream (-u) on push")


class GitCloneParams(BaseModel):
    url: str = Field(..., description="Repository URL to clone")
    dest: str = Field(..., description="Destination dir (relative to the workspace)")
    depth: Optional[int] = Field(None, description="Shallow-clone depth")


class GitTool(BaseTool):
    """Structured git over the confined workspace root. Gated by GIT_TOOLS_ENABLED."""

    def __init__(self, name: str = "git", config=None, container=None):
        super().__init__(name=name, config=config, container=container)
        self._root_override = None
        self._timeout = float(os.getenv("GIT_TOOL_TIMEOUT_SEC", "120"))

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

    def _confine(self, rel_path: str, root: str) -> str:
        target = os.path.abspath(os.path.join(root, rel_path))
        from core.path_safety import is_within_root
        if not is_within_root(target, root):
            raise GitError(f"path escapes the workspace root: {rel_path}")
        return target

    @staticmethod
    def _ok(content):
        from tools.controller.types import ActionResult
        return ActionResult(extracted_content=content)

    @staticmethod
    def _err(msg):
        from tools.controller.types import ActionResult
        return ActionResult(error=msg)

    @staticmethod
    def _unsafe_remote(value: str) -> bool:
        """True when ``value`` is a git transport-helper payload or flag injection.

        Git natively supports the ``ext::<command>`` transport (via git-remote-ext) and the
        related ``fd::<n>`` transport: as soon as the transport opens, git forks/execs
        ``<command>`` (or touches the given fd) on the HOST — this is RCE independent of our
        own argv-list hygiene, because the execution happens inside git's transport layer, not
        via ``shell=True`` in this module. Any ``transport::rest`` form (``ext::``, ``fd::``,
        or a custom ``url.<base>.insteadOf`` alias ending in ``::``) is blocked by the ``::``
        check. A leading ``-`` would let a "url"/"remote"/"ref" masquerade as a git flag
        (e.g. ``ref="--force"``), so it is refused too.
        """
        return "::" in value or value.startswith("-")

    # --- git runner ----------------------------------------------------------

    async def _run_git(self, args: List[str], execution_context=None, cwd: Optional[str] = None):
        """Run ``git -C <cwd or root> <args>`` in a thread; return (ok, text)."""
        import asyncio
        root = self._resolve_root(execution_context)
        workdir = cwd or root
        argv = ["git", "-C", workdir] + list(args)
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL") if k in os.environ}
        env.setdefault("GIT_TERMINAL_PROMPT", "0")  # never block on a credential prompt
        # Defense-in-depth: even though our own `_unsafe_remote` check rejects `::`-bearing
        # values before git ever runs, also tell git itself to refuse the `ext`/`fd`
        # transports (and anything else) natively — belt-and-suspenders in case a future
        # call site ever invokes `_run_git` with an unvalidated remote/url.
        env["GIT_ALLOW_PROTOCOL"] = "file:git:http:https:ssh"

        def _run_sync():
            import subprocess
            try:
                proc = subprocess.run(argv, cwd=workdir, env=env, capture_output=True, timeout=self._timeout)
                out = (proc.stdout or b"").decode("utf-8", "replace")
                err = (proc.stderr or b"").decode("utf-8", "replace")
                return proc.returncode, out, err
            except subprocess.TimeoutExpired:
                return 124, "", f"git timed out after {self._timeout:.0f}s"
            except FileNotFoundError:
                return 127, "", "git executable not found on PATH"

        loop = asyncio.get_event_loop()
        code, out, err = await loop.run_in_executor(None, _run_sync)
        text = out or ""
        if err:
            text = (text + ("\n" if text else "") + err)
        if code != 0:
            return False, (text or f"git exited {code}")
        return True, (text or "(ok)")

    # --- read actions --------------------------------------------------------

    @BaseTool.action("Show working-tree status (git status --porcelain)", param_model=GitStatusParams)
    async def git_status(self, params: GitStatusParams, execution_context=None):
        ok, text = await self._run_git(["status", "--porcelain=v1", "-b"], execution_context)
        return self._ok(text) if ok else self._err(text)

    @BaseTool.action("Show a diff of changes (optionally staged / path-limited)", param_model=GitDiffParams)
    async def git_diff(self, params: GitDiffParams, execution_context=None):
        args = ["diff"]
        if params.staged:
            args.append("--cached")
        if params.path:
            try:
                self._confine(params.path, self._resolve_root(execution_context))
            except GitError as e:
                return self._err(str(e))
            args += ["--", params.path]
        ok, text = await self._run_git(args, execution_context)
        return self._ok(text) if ok else self._err(text)

    @BaseTool.action("Show recent commit log (oneline)", param_model=GitLogParams)
    async def git_log(self, params: GitLogParams, execution_context=None):
        n = max(1, min(int(params.max_count or 20), 200))
        ok, text = await self._run_git(["log", f"-{n}", "--oneline"], execution_context)
        return self._ok(text) if ok else self._err(text)

    @BaseTool.action("List branches, or create a branch when 'name' is given", param_model=GitBranchParams)
    async def git_branch(self, params: GitBranchParams, execution_context=None):
        if params.name and params.name.startswith("-"):
            return self._err(f"refused: unsafe git branch name '{params.name}'")
        args = ["branch"] if not params.name else ["branch", params.name]
        ok, text = await self._run_git(args, execution_context)
        return self._ok(text) if ok else self._err(text)

    # --- write actions -------------------------------------------------------

    @BaseTool.action("Checkout a branch/commit (create with -b when create=true)", param_model=GitCheckoutParams)
    async def git_checkout(self, params: GitCheckoutParams, execution_context=None):
        if params.ref.startswith("-"):
            return self._err(f"refused: unsafe git ref '{params.ref}'")
        args = ["checkout"] + (["-b"] if params.create else []) + [params.ref]
        ok, text = await self._run_git(args, execution_context)
        return self._ok(text) if ok else self._err(text)

    @BaseTool.action("Stage paths (git add)", param_model=GitAddParams)
    async def git_add(self, params: GitAddParams, execution_context=None):
        if not params.paths:
            return self._err("no paths to add")
        root = self._resolve_root(execution_context)
        for p in params.paths:
            try:
                self._confine(p, root)
            except GitError as e:
                return self._err(str(e))
        ok, text = await self._run_git(["add", "--"] + list(params.paths), execution_context)
        return self._ok(text) if ok else self._err(text)

    @BaseTool.action("Commit staged changes (git commit -m)", param_model=GitCommitParams)
    async def git_commit(self, params: GitCommitParams, execution_context=None):
        args = ["commit", "-m", params.message] + (["-a"] if params.all else [])
        ok, text = await self._run_git(args, execution_context)
        return self._ok(text) if ok else self._err(text)

    @BaseTool.action("Pull from a remote (git pull)", param_model=GitPullParams)
    async def git_pull(self, params: GitPullParams, execution_context=None):
        if self._unsafe_remote(params.remote):
            return self._err(f"refused: unsafe git url/remote '{params.remote}'")
        args = ["pull", params.remote] + ([params.branch] if params.branch else [])
        ok, text = await self._run_git(args, execution_context)
        return self._ok(text) if ok else self._err(text)

    @BaseTool.action("Push to a remote (git push) — high-impact, approval-gated", param_model=GitPushParams)
    async def git_push(self, params: GitPushParams, execution_context=None):
        if self._unsafe_remote(params.remote):
            return self._err(f"refused: unsafe git url/remote '{params.remote}'")
        args = ["push"] + (["-u"] if params.set_upstream else []) + [params.remote]
        if params.branch:
            args.append(params.branch)
        ok, text = await self._run_git(args, execution_context)
        return self._ok(text) if ok else self._err(text)

    @BaseTool.action("Clone a repository into a confined workspace subdir (git clone)", param_model=GitCloneParams)
    async def git_clone(self, params: GitCloneParams, execution_context=None):
        if self._unsafe_remote(params.url):
            return self._err(f"refused: unsafe git url/remote '{params.url}'")
        try:
            root = self._resolve_root(execution_context)
            dest_abs = self._confine(params.dest, root)
        except GitError as e:
            return self._err(str(e))
        args = ["clone"]
        if params.depth:
            args += ["--depth", str(int(params.depth))]
        args += [params.url, dest_abs]
        ok, text = await self._run_git(args, execution_context, cwd=root)
        return self._ok(text) if ok else self._err(text)
