"""Helpers the ship rail's tools share: publish, app_service, hf_deploy.

Each of the three tools carried its own copy of how to reach the session's
orchestrator, which approver gates a FIRST publish, and where the session
workspace is. Three copies of a policy are three places for it to drift; the
policy lives here and the tools keep thin methods over it (their names are
test seams).
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional


def resolve_orchestrator(container: Any, session_id: str,
                         resolver: Optional[Callable[[str], Any]] = None):
    """The live orchestrator for *session_id*, or None. An injected *resolver*
    (a test seam) always wins; otherwise the container's ``task_agent``.
    *container* may be a zero-arg getter (``lambda: self.container``) — the
    BaseComponent property raises when no container was ever bound, and that
    must resolve to None here, not propagate."""
    if resolver is not None:
        try:
            return resolver(session_id)
        except Exception:
            return None
    try:
        if callable(container) and not hasattr(container, "get_service"):
            container = container()
        agent = None
        if container is not None:
            if hasattr(container, "get_agent"):
                agent = container.get_agent("task_agent")
            if agent is None and hasattr(container, "get_service"):
                agent = container.get_service("task_agent")
        return agent.get_orchestrator(session_id) if agent else None
    except Exception:
        return None


def first_publish_approval_provider():
    """The approver that gates a FIRST publish of an unknown app/slug.

    Resolve the SAME provider the Controller uses at posture>=2 via
    ``resolve_gated_actions`` — ``interactive_cli`` by default, NOT
    ``AutoApprover`` — so a brand-new PUBLIC address can never go live from an
    unattended headless run (``interactive_cli`` fail-closes when it cannot
    prompt). An already-approved app skips this path entirely.

    013 T4 review (Finding 1): under effective ``AUTONOMY_MODE=autonomous``
    the gated set's provider defaults to ``auto_notify`` (allow + audit +
    post-hoc notify). ``AutoNotifyApprover.request()`` always returns True, so
    honoring it here would silently first-publish from an unattended run —
    inverting the invariant. It is remapped to the durable, remotely-approvable
    ``owner_queue`` instead: a real owner can still approve out-of-band (e.g.
    Telegram ``/approve``), so autonomous mode does not FREEZE the rail, but
    it never rubber-stamps — ``owner_queue`` itself fail-closes for a
    forged/leaf/sub-agent/autonomous-goal-run turn. An explicit
    non-``auto_notify`` resolution (``deny``, an operator-set ``owner_queue``
    or custom provider, supervised ``interactive_cli``) is untouched.
    """
    # Importing this registers the 'interactive_cli' provider so it can be
    # resolved (mirrors Controller.__init__'s H9 import).
    try:
        import tools.controller.approval_interactive  # noqa: F401
    except Exception:
        pass
    from tools.controller.approval import (
        get_approval_provider_or_deny, resolve_gated_actions,
    )
    _required, provider_name = resolve_gated_actions()
    if provider_name == "auto_notify":
        try:
            import tools.controller.approval_queue  # noqa: F401 — registers 'owner_queue'
        except Exception:
            pass
        provider_name = "owner_queue"
    return get_approval_provider_or_deny(provider_name)


def session_workspace_root(execution_context: Any, *, override: Optional[str] = None,
                           fallback_cwd: bool = False) -> Optional[str]:
    """The session workspace a tool may read from or write into.

    *override* (a test seam / the local CLI's project root) wins; then the
    context's ``workspace_dir``; then the path manager's per-session,
    per-TENANT workspace. With ``fallback_cwd=False`` (the ship rail) the
    answer is None when nothing resolves — on a shared project-root workspace
    the cwd would be the whole install tree, which is never something to
    publish. The coding/git tools pass ``fallback_cwd=True`` because on the
    local CLI the working directory IS the project.
    """
    if override:
        return os.path.abspath(override)
    ws = getattr(execution_context, "workspace_dir", None)
    if ws:
        return str(ws)
    try:
        sid = getattr(execution_context, "session_id", None)
        if sid or not fallback_cwd:
            from agents.task.path import pm
            return str(pm().get_workspace_dir(
                sid or "", getattr(execution_context, "user_id", None)))
    except Exception:
        pass
    return os.getcwd() if fallback_cwd else None
