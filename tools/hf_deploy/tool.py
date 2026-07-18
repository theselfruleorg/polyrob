"""``hf_deploy`` agent tool (proposal §3): publish the session workspace as a
Hugging Face Space.

LANDMINE: NO ``from __future__ import annotations`` in this module — it holds
the action closures whose param models the Registry introspects (stringized
annotations break that routing).

Gating (proposal §3.4): ``AGENT_COMPUTE_POSTURE>=2``, owner tenant, not a
leaf/sub-agent/forged (self_wake/delegation_result) turn — see
``_deny_reason``, built on the existing ``compute_posture_allows`` gate.
First publish of a NEW app name is gated by a REAL approving provider — the
tool resolves the SAME interactive-default provider the Controller uses at
posture>=2 (``resolve_gated_actions``), so an unattended/headless run cannot
first-publish a brand-new PUBLIC app (interactive_cli fail-closes to deny when
it can't prompt). Once approved (registry-backed, §3.5), a redeploy of that
SAME app runs unattended within ``HF_DEPLOY_DAILY_MAX``/
``HF_DEPLOY_MIN_INTERVAL_SEC`` (it skips the approver entirely). Every deploy
is additionally gated on a green ``run_tests`` with no edit since (the
ship==tested acceptance-contract leg, §3.6) via
``tools.hf_deploy.digest.tested_tree_digest``.

The ``HF_TOKEN`` never appears in a param, an ``ActionResult``, or a log line
— see ``tools/hf_deploy/broker.py``'s token-custody contract.
"""
import logging
import os
import re
import time
import types
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool
from tools.controller.types import ActionResult
from tools.hf_deploy.broker import BrokerError, HFSpacesBroker
from tools.hf_deploy.digest import tested_tree_digest
from tools.hf_deploy import hf_deploy_daily_max, hf_deploy_min_interval_sec

import agents.task.telemetry.self_events as self_events

logger = logging.getLogger(__name__)

# 2-63 char lowercase slug: letters/digits/single hyphens, no leading/trailing
# hyphen, no consecutive hyphens, no dot/space/slash/uppercase/traversal.
_APP_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _valid_app_name(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    if not (2 <= len(name) <= 63):
        return False
    if name != name.lower():
        return False
    if any(c in name for c in ("..", "/", " ", ".")):
        return False
    return bool(_APP_NAME_RE.match(name))


def _emit_event(kind: str, execution_context, attrs: Dict[str, Any]) -> None:
    """First-class hf_deploy telemetry (fail-open). Module-level so tests can
    monkeypatch this exact seam."""
    try:
        from agents.task.telemetry.event_log import event_log_enabled, get_event_log
        if not event_log_enabled():
            return
        uid = getattr(execution_context, "user_id", "") or ""
        sid = getattr(execution_context, "session_id", "") or ""
        get_event_log().record(kind, user_id=uid, session_id=sid,
                               source="hf_deploy", attrs=attrs or {})
    except Exception as e:
        logger.debug("hf_deploy event emit skipped: %s", e)


def _deny_reason(execution_context) -> Optional[str]:
    """Non-None -> deny with this message. Built on the existing
    ``compute_posture_allows`` gate (posture, owner tenant, leaf/sub-agent,
    forged self_wake/delegation_result turn_kind) plus a specific message for
    the posture shortfall case."""
    if execution_context is None:
        return "hf_deploy requires an execution context"
    from core.config_policy import compute_posture, compute_posture_allows
    posture = compute_posture()
    if posture < 2:
        return (f"hf_deploy requires AGENT_COMPUTE_POSTURE>=2 (self-maintain tier); "
                f"current AGENT_COMPUTE_POSTURE is {posture}")
    if not compute_posture_allows(execution_context, 2):
        return "hf_deploy denied: not an owner-tenant orchestrator turn"
    return None


class DeployParams(BaseModel):
    app_name: str = Field(..., description="Space name (2-63 char lowercase slug: letters, "
                                            "digits, single hyphens). The Space becomes "
                                            "<HF_DEPLOY_ORG>/<app_name>.")
    health_path: Optional[str] = Field(default="/", description="Path checked for a 2xx "
                                       "response after deploy (e.g. '/health').")
    secrets: Optional[Dict[str, str]] = Field(default=None, description="Environment "
                                              "secrets to set on the Space (e.g. API keys "
                                              "the deployed app needs at runtime).")


class UndeployParams(BaseModel):
    app_name: str = Field(..., description="The app_name of a previously-deployed Space to delete.")


class ListDeploymentsParams(BaseModel):
    pass


class HFDeployTool(BaseTool):
    """Deploy the session workspace to a Hugging Face Space (Docker SDK).

    Gated ``AGENT_COMPUTE_POSTURE>=2`` + owner tenant; a first publish of a new
    app needs an approving provider (interactive default, denied headless) while
    an already-approved app redeploys unattended within caps; every deploy
    requires a green, untouched ``run_tests`` (ship == tested).
    """

    def __init__(self, name: str = "hf_deploy", config=None, container=None):
        if config is None:
            config = types.SimpleNamespace()
        super().__init__(name=name, config=config, container=container)
        self._registry = None
        self._broker = None
        self._approval_provider = None
        self._orchestrator_resolver = None
        self._workspace_root_override = None

    # --- lazy collaborator resolution (test seams override the attrs directly) ---

    def _get_registry(self):
        if self._registry is None:
            from tools.hf_deploy.registry import DeployedAppsRegistry, default_deployed_apps_db
            self._registry = DeployedAppsRegistry(default_deployed_apps_db())
        return self._registry

    def _get_broker(self):
        if self._broker is None:
            self._broker = HFSpacesBroker()
        return self._broker

    def _get_approval_provider(self):
        """The approver that gates a FIRST publish of an unknown app.

        The injected ``_approval_provider`` test seam always wins. In production
        (nothing injected) we resolve the SAME provider the Controller uses at
        posture>=2 via ``resolve_gated_actions`` — i.e. ``interactive_cli`` by
        default, NOT ``AutoApprover``. So a brand-new PUBLIC app can never be
        first-published from an unattended headless run (interactive_cli
        fail-closes to deny when it can't prompt). An already-approved app skips
        this path entirely (``deploy`` only asks when ``needs_approval``), so its
        redeploy stays unattended within caps.

        013 T4 review (Finding 1): under effective ``AUTONOMY_MODE=autonomous``,
        ``resolve_gated_actions()`` now defaults the gated set's provider to
        ``auto_notify`` (allow + audit + post-hoc owner notify — the generic
        act-and-report lane wired in ``tools/controller/service.py``).
        ``AutoNotifyApprover.request()`` always returns ``True``, so honoring it
        HERE would silently first-publish a brand-new PUBLIC HF Space from an
        unattended run — inverting the invariant documented above and at module
        scope (:13). ``auto_notify`` is therefore remapped to the durable,
        remotely-approvable ``owner_queue`` provider instead of ``interactive_cli``:
        a real owner can still approve a first publish out-of-band (e.g. Telegram
        ``/approve``), so autonomous mode doesn't FREEZE headless hf_deploy, but it
        never rubber-stamps — ``owner_queue`` itself fail-closes (denies, no ask
        even created) for a forged/leaf/sub-agent/autonomous-goal-run turn
        (``tools/controller/approval_queue.py::OwnerQueueApprover.request``),
        exactly the shape ``interactive_cli`` fails closed for on a headless run.
        An explicit non-``auto_notify`` resolution (``deny``, an operator-set
        ``owner_queue``/custom provider, or supervised-mode ``interactive_cli``) is
        untouched.
        """
        if self._approval_provider is None:
            # Importing this registers the 'interactive_cli' provider so it can
            # actually be resolved (mirrors Controller.__init__'s H9 import).
            try:
                import tools.controller.approval_interactive  # noqa: F401
            except Exception:
                pass
            from tools.controller.approval import (
                get_approval_provider_or_deny, resolve_gated_actions,
            )
            _required, provider_name = resolve_gated_actions()
            if provider_name == "auto_notify":
                # Finding 1: never let the generic allow-all act-and-report lane
                # gate a first PUBLIC publish — fall back to the durable
                # owner-approval queue instead.
                try:
                    import tools.controller.approval_queue  # noqa: F401 — registers 'owner_queue'
                except Exception:
                    pass
                provider_name = "owner_queue"
            self._approval_provider = get_approval_provider_or_deny(provider_name)
        return self._approval_provider

    def _resolve_workspace_root(self, execution_context):
        """Resolve the session workspace dir to publish, or None when it can't
        be resolved. NEVER falls back to ``os.getcwd()`` — a cwd fallback on
        headless prod is the install tree (/opt/polyrob: source + config), and
        this tool PUBLISHES the resolved dir to a PUBLIC HF Space. The caller
        REFUSES on None rather than shipping the wrong directory."""
        if self._workspace_root_override:
            return self._workspace_root_override
        try:
            sid = getattr(execution_context, "session_id", None)
            if sid:
                from agents.task.path import pm
                return str(pm().get_workspace_dir(sid, getattr(execution_context, "user_id", None)))
        except Exception:
            return None
        return None

    def _resolve_orchestrator(self, session_id):
        resolver = self._orchestrator_resolver
        if resolver is not None:
            try:
                return resolver(session_id)
            except Exception:
                return None
        try:
            agent = None
            if self.container is not None:
                if hasattr(self.container, "get_agent"):
                    agent = self.container.get_agent("task_agent")
                if agent is None and hasattr(self.container, "get_service"):
                    agent = self.container.get_service("task_agent")
            return agent.get_orchestrator(session_id) if agent else None
        except Exception:
            return None

    # --- actions -------------------------------------------------------------

    @BaseTool.action(
        "Deploy the current session workspace as a Hugging Face Space (Docker SDK). "
        "The FIRST publish of a new app name requires an approving provider "
        "(interactive by default; denied on an unattended/headless run). Once an "
        "app is approved, later redeploys of that SAME app run unattended within the "
        "configured daily/interval caps. Requires a green run_tests with no edits "
        "since (ship == tested).",
        param_model=DeployParams,
    )
    async def deploy(self, params: DeployParams, execution_context=None) -> ActionResult:
        deny = _deny_reason(execution_context)
        if deny:
            return ActionResult(error=deny, include_in_memory=True)

        app_name = params.app_name
        if not _valid_app_name(app_name):
            return ActionResult(
                error=f"invalid app_name {app_name!r}: must be a 2-63 char lowercase slug "
                      "(letters, digits, single hyphens; no dots/spaces/slashes/uppercase)",
                include_in_memory=True)

        org = (os.environ.get("HF_DEPLOY_ORG") or "").strip()
        if not org:
            return ActionResult(error="HF_DEPLOY_ORG is not configured", include_in_memory=True)
        if not HFSpacesBroker.resolve_token():
            return ActionResult(error="HF_TOKEN is not configured", include_in_memory=True)

        uid = getattr(execution_context, "user_id", "") or ""
        sid = getattr(execution_context, "session_id", "") or ""

        workspace = self._resolve_workspace_root(execution_context)
        if not workspace:
            # NEVER fall back to cwd (= install tree on headless prod). Refuse
            # rather than risk publishing the wrong directory publicly.
            return ActionResult(
                error="could not resolve session workspace to publish; refusing to deploy",
                include_in_memory=True)

        orch = self._resolve_orchestrator(sid)
        digest, reason = tested_tree_digest(orch, workspace)
        if reason:
            return ActionResult(error=reason, include_in_memory=True)

        registry = self._get_registry()

        min_interval = hf_deploy_min_interval_sec()
        last_attempt = registry.last_attempt_epoch(app_name, uid)
        if last_attempt is not None and (time.time() - last_attempt) < min_interval:
            return ActionResult(
                error=f"HF_DEPLOY_MIN_INTERVAL_SEC not elapsed for '{app_name}' "
                      f"({min_interval}s minimum between deploys of the same app)",
                include_in_memory=True)

        daily_max = hf_deploy_daily_max()
        if registry.deploys_in_last_day(uid) >= daily_max:
            return ActionResult(
                error=f"HF_DEPLOY_DAILY_MAX ({daily_max}) reached for today",
                include_in_memory=True)

        space_repo = f"{org}/{app_name}"
        row = registry.get(app_name, uid)
        needs_approval = row is None or row.get("approved_at") is None
        if needs_approval:
            registry.upsert_pending(app_name, uid, space_repo=space_repo)
            approver = self._get_approval_provider()
            try:
                approved = await approver.request(
                    "hf_deploy_publish",
                    {"app_name": app_name, "space_repo": space_repo},
                    execution_context,
                )
            except Exception as e:
                return ActionResult(
                    error=f"approval denied (provider error) for first publish of '{app_name}': {e}",
                    include_in_memory=True)
            if not approved:
                return ActionResult(
                    error=f"approval denied for first publish of '{app_name}'",
                    include_in_memory=True)
            registry.mark_approved(app_name, uid)

        registry.record_attempt(app_name, uid)
        broker = self._get_broker()
        try:
            url = await broker.deploy_space(
                space_repo=space_repo, workspace_dir=workspace, secrets=params.secrets)
        except BrokerError as e:
            registry.record_failed(app_name, uid, error=str(e))
            return ActionResult(error=str(e), include_in_memory=True)

        health_path = params.health_path or "/"
        check_url = f"{url}{health_path}"
        try:
            healthy = await broker.health_check(check_url)
        except Exception:
            healthy = False
        if not healthy:
            registry.record_failed(app_name, uid, error=f"health check failed: {check_url}")
            return ActionResult(
                error=f"health check failed: {check_url} did not return 2xx after deploy",
                include_in_memory=True)

        registry.record_live(app_name, uid, space_repo=space_repo, public_url=url,
                             health_path=health_path, workspace_digest=digest)
        self_events.emit_self_modification(
            kind="hf_deploy", action="deploy", item_id=app_name,
            user_id=uid, session_id=sid, ok=True, source="hf_deploy_tool")
        _emit_event("app_deployed", execution_context, {"app_name": app_name, "url": url})

        return ActionResult(
            extracted_content=f"Deployed '{app_name}' -> {url}",
            include_in_memory=True,
        )

    @BaseTool.action(
        "Undeploy (delete) a previously-deployed Hugging Face Space.",
        param_model=UndeployParams,
    )
    async def undeploy(self, params: UndeployParams, execution_context=None) -> ActionResult:
        deny = _deny_reason(execution_context)
        if deny:
            return ActionResult(error=deny, include_in_memory=True)

        uid = getattr(execution_context, "user_id", "") or ""
        sid = getattr(execution_context, "session_id", "") or ""
        registry = self._get_registry()
        row = registry.get(params.app_name, uid)
        if row is None:
            return ActionResult(error=f"unknown app '{params.app_name}'", include_in_memory=True)

        org = (os.environ.get("HF_DEPLOY_ORG") or "").strip()
        space_repo = row.get("space_repo") or f"{org}/{params.app_name}"
        broker = self._get_broker()
        try:
            await broker.delete_space(space_repo=space_repo)
        except BrokerError as e:
            return ActionResult(error=str(e), include_in_memory=True)

        registry.set_status(params.app_name, uid, "undeployed")
        self_events.emit_self_modification(
            kind="hf_deploy", action="undeploy", item_id=params.app_name,
            user_id=uid, session_id=sid, ok=True, source="hf_deploy_tool")
        return ActionResult(extracted_content=f"Undeployed '{params.app_name}'.",
                            include_in_memory=True)

    @BaseTool.action(
        "List your deployed Hugging Face Spaces and their status.",
        param_model=ListDeploymentsParams,
    )
    async def list_deployments(self, params: ListDeploymentsParams,
                               execution_context=None) -> ActionResult:
        deny = _deny_reason(execution_context)
        if deny:
            return ActionResult(error=deny, include_in_memory=True)

        uid = getattr(execution_context, "user_id", "") or ""
        registry = self._get_registry()
        rows = registry.list_for(uid)
        if not rows:
            return ActionResult(extracted_content="No deployed apps.", include_in_memory=True)
        lines = [
            f"- {r['app_name']} [{r['status']}] {r.get('public_url') or ''}".rstrip()
            for r in rows
        ]
        return ActionResult(extracted_content="Deployed apps:\n" + "\n".join(lines),
                            include_in_memory=True)
