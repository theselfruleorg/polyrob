"""The `self_env` self-maintenance tool (WS-5, posture 2). NO ``from __future__
import annotations`` — the action closures' Pydantic param models are introspected
by the Registry (the agent-upgrades-wave4 / GLM param_model landmine)."""
import logging
import os
import re
from pathlib import Path
from typing import Optional, Tuple

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.controller.types import ActionResult

# Conservative pip requirement-spec shape. Dependency changes are proposals only;
# a trusted updater resolves and installs a reviewed lock delta.
_PKG_SPEC_RE = re.compile(r"^[A-Za-z0-9._+\-\[\],]+(?:[=<>!~]=?[A-Za-z0-9._*+!,<>=]*)?$")
_EXACT_PIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-]*(?:\[[A-Za-z0-9._+\-]+\])?==[A-Za-z0-9][A-Za-z0-9._+\-!]*$")
_READ_CAP = 100_000  # bytes returned by read_source


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
            from core.security.host_execution import host_execution_refusal
            if host_execution_refusal():
                return False
            from core.config_policy import compute_posture_allows
            return bool(compute_posture_allows(execution_context, 2))
        except Exception:
            return False

    def _deny(self) -> ActionResult:
        return ActionResult(error=(
            "self_env requires the self-maintain compute posture "
            "(AGENT_COMPUTE_POSTURE>=2) and an owner-steered, non-delegated turn; "
            "each verb is also approval-gated. Self-maintenance is disabled in wallet custody processes."
        ))

    def _emit(self, *, action: str, item_id: str = "", ok: bool = True,
              execution_context=None, **attrs) -> None:
        """Emit a self_modification audit event (fail-open)."""
        try:
            from core.self_events import emit_self_modification
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
        from core.security.secret_guard import (
            is_credential_file, is_protected_config_path, is_secret_path,
        )
        if (is_credential_file(target) or is_protected_config_path(target)
                or is_secret_path(target, root=root)):
            return None, f"refusing to touch a credential/secret/config file: {rel_path}"
        # 018 P4: config-DEFAULT source is protected like config FILES. The
        # posture/approval ladder is defined in core/config_policy/ — patching
        # e.g. _resolve_compute_posture would be a code-level flag flip that the
        # env/config file denials above cannot see (source-vs-config asymmetry
        # from the 2026-07-17 config review).
        if "config_policy" in target.parts:
            return None, ("refusing to patch capability-policy source "
                          f"(core/config_policy/): {rel_path}")
        # 024 review I7 (same asymmetry as config_policy above): patching the
        # guard itself (core/security/ — is_credential_file → return False) or
        # the credential layer (core/llm_auth/ — AuthStore.load echoing tokens)
        # would be a code-level bypass of every file denial in this function.
        parts = set(target.parts)
        if "security" in parts and "core" in parts:
            return None, ("refusing to patch the security-guard source "
                          f"(core/security/): {rel_path}")
        if "wallet" in parts and "core" in parts:
            return None, f"refusing to patch wallet authority source: {rel_path}"
        if "llm_auth" in parts:
            return None, ("refusing to patch the credential-layer source "
                          f"(core/llm_auth/): {rel_path}")
        return target, None

    @staticmethod
    def _proposal_store():
        from core.maintenance_proposals import MaintenanceProposalStore
        return MaintenanceProposalStore.from_runtime()

    # --- verbs ---------------------------------------------------------------

    @BaseTool.action(
        "Propose one exact pinned dependency for trusted maintenance review (does not install it)",
        param_model=InstallDepParams,
    )
    async def self_env_install_dep(self, params: InstallDepParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        pkg = (params.package or "").strip()
        if (not pkg or pkg.startswith("-") or not _PKG_SPEC_RE.fullmatch(pkg)
                or not _EXACT_PIN_RE.fullmatch(pkg)):
            return ActionResult(error=f"invalid package spec (a package name, not a pip flag): {params.package!r}")
        try:
            proposal = self._proposal_store().propose_dependency(
                package=pkg, user_id=getattr(execution_context, "user_id", ""),
                session_id=getattr(execution_context, "session_id", ""))
        except Exception as exc:
            self._emit(action="install_dep", item_id=pkg, ok=False, execution_context=execution_context)
            return ActionResult(error=f"dependency proposal failed: {exc}")
        self._emit(action="install_dep", item_id=proposal["proposal_id"], ok=True,
                   execution_context=execution_context, proposed=True)
        return ActionResult(extracted_content=(
            f"Dependency proposal `{proposal['proposal_id']}` recorded for `{pkg}`. "
            "No package was installed; a trusted updater must resolve a locked environment, "
            "validate it, and apply the approved digest."), include_in_memory=True)

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
            from core.security.confined_read import read_confined_bytes
            text = read_confined_bytes(target, self._install_root(), _READ_CAP).decode("utf-8", "replace")
        except Exception as e:
            return ActionResult(error=f"read failed: {e}")
        if len(text) > _READ_CAP:
            text = text[:_READ_CAP] + f"\n...[truncated {len(text) - _READ_CAP} chars]"
        self._emit(action="read_source", item_id=params.path, ok=True,
                   execution_context=execution_context)
        return ActionResult(extracted_content=text)

    @BaseTool.action(
        "Propose a unique source replacement for trusted maintenance review (does not edit live code)",
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
            from core.security.confined_read import read_confined_bytes
            content = read_confined_bytes(target, self._install_root(), 512 * 1024).decode("utf-8")
            count = content.count(params.old_string)
            if count == 0:
                return ActionResult(error=f"old_string not found in {params.path}")
            if count > 1:
                return ActionResult(error=f"old_string is not unique in {params.path} ({count} matches)")
            candidate = content.replace(params.old_string, params.new_string)
            proposal = self._proposal_store().propose_source_patch(
                path=target.relative_to(self._install_root()).as_posix(), base_content=content,
                candidate_content=candidate, user_id=getattr(execution_context, "user_id", ""),
                session_id=getattr(execution_context, "session_id", ""))
        except Exception as e:
            self._emit(action="patch_source", item_id=params.path, ok=False,
                       execution_context=execution_context)
            return ActionResult(error=f"patch failed: {e}")
        self._emit(action="patch_source", item_id=proposal["proposal_id"], ok=True,
                   execution_context=execution_context, proposed=True)
        return ActionResult(extracted_content=(
            f"Source patch proposal `{proposal['proposal_id']}` recorded for `{params.path}` "
            f"(base `{proposal['base_sha256']}`, candidate `{proposal['candidate_sha256']}`). "
            "Live code is unchanged; a trusted updater must verify and apply these exact bytes."),
            include_in_memory=True)

    @BaseTool.action(
        "Explain that repository updates require the trusted update supervisor",
        param_model=GitPullParams,
    )
    async def self_env_git_pull(self, params: GitPullParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        self._emit(action="git_pull", item_id="", ok=False, execution_context=execution_context,
                   reason="trusted_updater_required")
        return ActionResult(error=("git pull is not available to the running agent. "
                                   "Use the trusted `polyrob update` supervisor path, which stages, "
                                   "validates, health-checks, and rolls back code."))

    @BaseTool.action(
        "Explain that restarts require the trusted supervisor after a verified update",
        param_model=RestartParams,
    )
    async def self_env_restart_service(self, params: RestartParams, execution_context=None):
        if not self._allowed(execution_context):
            return self._deny()
        self._emit(action="restart_service", item_id="", ok=False, execution_context=execution_context,
                   reason="trusted_supervisor_required")
        return ActionResult(error=("restart is not available to the running agent. A trusted supervisor may "
                                   "restart only after it has durably recorded the owner-visible result and "
                                   "verified the approved update."))
