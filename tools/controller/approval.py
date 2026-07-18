"""Tool-call approval seam (Item 7E — minimal).

An ``ApprovalProvider`` decides whether a gated action may run. ``make_approval_hook``
turns a provider + a set of gated action names into a **pre-tool-call hook**: for an
action in the gated set it asks the provider and DENIES on ``False`` / timeout /
error; un-gated actions always pass. Register the hook ``fail_mode="closed"`` so a
crash also denies.

This is mechanism only — no UI. The default ``AutoApprover`` preserves current
behaviour (everything allowed); wire a real interactive/remote provider in later.

✅ **Async pipeline (UP-04).** The hook pipeline is now ``async`` end-to-end, so the
pre-tool-call hook ``await``s the provider **directly** (no ``run_coroutine_sync``
bridge) — a slow/interactive/network provider yields the event loop instead of
freezing it, and concurrent sessions/sub-agents keep progressing while one action
waits on approval. The wait is bounded by ``asyncio.wait_for(..., APPROVAL_TIMEOUT_SEC)``.

⚠️ **Cancellation contract.** On timeout, ``asyncio.wait_for`` **cancels** the
in-flight ``provider.request`` coroutine (raises ``CancelledError`` inside it). A real
provider that holds a resource (an open prompt, a network request, a staged decision
row) MUST be cancellation-safe — release/clean up in a ``finally`` or
``except asyncio.CancelledError``. The shipped providers hold nothing, so they are
trivially safe. (This is strictly better than the old bridge, which left a timed-out
coroutine orphaned on a background loop.)

Env wiring (in ``Controller.__init__``):
  - ``APPROVAL_REQUIRED_TOOLS`` — comma list of action names to gate (default empty = no-op)
  - ``APPROVAL_PROVIDER`` — ``auto`` (default) | ``deny`` | custom-registered name
"""
from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

DEFAULT_APPROVAL_TIMEOUT_SEC = float(os.getenv("APPROVAL_TIMEOUT_SEC", "30"))


# WS-7: the approval-gating flags are FROZEN at import — snapshotted once so a
# prompt-injected mid-process env mutation can never widen/narrow the gated set or
# swap the provider mid-session. Operators set these in real process env at startup.
# (Mirrors the AGENT_COMPUTE_POSTURE freeze in agents/task/constants.py.)
def _snapshot_required_tools() -> frozenset:
    raw = (os.getenv("APPROVAL_REQUIRED_TOOLS", "") or "").strip()
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


_FROZEN_APPROVAL_REQUIRED_TOOLS = _snapshot_required_tools()
_FROZEN_APPROVAL_PROVIDER = (os.getenv("APPROVAL_PROVIDER", "auto") or "auto").strip() or "auto"


def frozen_approval_required_tools() -> frozenset:
    """Import-time snapshot of ``APPROVAL_REQUIRED_TOOLS`` (WS-7 — mutation-proof)."""
    return _FROZEN_APPROVAL_REQUIRED_TOOLS


def frozen_approval_provider() -> str:
    """Import-time snapshot of ``APPROVAL_PROVIDER`` (WS-7 — mutation-proof)."""
    return _FROZEN_APPROVAL_PROVIDER


def resolve_gated_actions() -> tuple:
    """WS-6: the effective ``(gated_action_names: set, provider_name: str)`` for a
    Controller, given the frozen operator config and the (frozen) compute posture.

    Posture < 2: exactly the operator's ``APPROVAL_REQUIRED_TOOLS`` + their provider
    (byte-identical to pre-WS-6). Posture >= 2 (self-maintenance tier): UNION the
    recommended coding/self-evolution set (:data:`DEFAULT_APPROVAL_REQUIRED_TOOLS`)
    with the compute verbs (:data:`POSTURE2_APPROVAL_REQUIRED_TOOLS` — shell_run +
    self_env_*), and default the provider to ``interactive_cli`` when the operator
    left it ``auto``/empty (an explicit provider still wins; the interactive one
    fail-closes to deny when it can't prompt, e.g. headless). Pure — resolution only,
    no hook registration.
    """
    required = set(frozen_approval_required_tools())
    provider = frozen_approval_provider()
    try:
        from core.config_policy import compute_posture
        if compute_posture() >= 2:
            required |= set(DEFAULT_APPROVAL_REQUIRED_TOOLS)
            required |= set(POSTURE2_APPROVAL_REQUIRED_TOOLS)
            if provider in ("", "auto"):
                provider = "interactive_cli"
    except Exception as e:
        # Don't silently drop the posture-2 approval tightening — log it. (The
        # per-tool compute_posture_allows(ctx,2) guard is the primary control; this
        # union is defense-in-depth, so we still fail-open on the union rather than
        # break Controller construction.)
        logger.error("resolve_gated_actions: posture-2 union failed (%s); "
                     "posture-2 approval tightening NOT applied", e)
    try:
        # 013 T4: act-and-report under effective AUTONOMY_MODE=autonomous — an
        # unset/auto/interactive default becomes `auto_notify` (allow + audit +
        # post-hoc owner notify). An explicit deny/owner_queue/custom env provider
        # still wins; supervised mode is byte-identical (full_autonomy_enabled()
        # is False). The always-owner-queued lane (`_ALWAYS_GATED_VERBS` + owner
        # pref pins) is split off later, at the Controller wiring, via
        # `autonomous_gating_lanes`.
        from core.config_policy import full_autonomy_enabled
        if full_autonomy_enabled() and provider in ("", "auto", "interactive_cli"):
            provider = "auto_notify"
    except Exception as e:
        logger.error("resolve_gated_actions: autonomy-mode provider default "
                     "failed (%s); supervised provider kept", e)
    return required, provider


def pref_gated_actions(user_id: Optional[str], home_dir) -> frozenset:
    """Session-construction-time pref ADDITIONS to the gated set (owner-UX P1 T5).

    Pure ADDITIVE union: an ``approvals.require`` preference can only ADD action
    names to the operator's gated set, never remove one — the import-frozen env
    snapshot (:func:`frozen_approval_required_tools`) is never consulted or
    mutated here, and the caller is expected to UNION this result into whatever
    :func:`resolve_gated_actions` already returned. No pref file (or an empty
    ``approvals.require`` list) => ``frozenset()`` => no-op.
    """
    from core import prefs
    req = prefs.resolve(
        "approvals.require", user_id, home_dir, env_value=[], default=[]
    ) or []
    return frozenset(req)


def effective_approval_state(user_id: Optional[str], home_dir) -> tuple:
    """The single source of truth for "what is gated, from where, and by which
    provider" (owner-UX P2 T4).

    Extracted from ``Controller.__init__``'s approval-seam composition so the
    `/approve` REPL command + `polyrob approvals` CLI can display EXACTLY what
    ``Controller.__init__`` enforces — display can never drift from
    enforcement because both call this one function.

    Returns ``(gates, provider)``:
      - ``gates``: ``{action_name: source}`` where ``source`` is one of
        ``"env (frozen)"`` (the operator's frozen ``APPROVAL_REQUIRED_TOOLS``),
        ``"posture"`` (the posture>=2 recommended coding/self-evolution +
        compute-verb union — see :func:`resolve_gated_actions`), or ``"pref"``
        (an owner-added ``approvals.require`` preference entry not already
        covered by env/posture). An action gated by more than one source shows
        the highest-priority one (env > posture > pref).
      - ``provider``: the fully-resolved effective provider name — the
        env/posture default, further tightened by an ``approvals.provider``
        preference (stricter-of-two, never looser; see
        ``core.prefs``'s ``stricter_provider`` merge).

    Fail-open: any pref-resolution error degrades to the env+posture-only view
    (byte-identical to no ``preferences.toml`` present) — never raises.
    """
    required, provider = resolve_gated_actions()
    env_set = set(frozen_approval_required_tools())
    # Anything resolve_gated_actions() added beyond the frozen env snapshot is
    # posture-derived (it only ever unions DEFAULT_APPROVAL_REQUIRED_TOOLS /
    # POSTURE2_APPROVAL_REQUIRED_TOOLS at compute posture >= 2).
    posture_set = set(required) - env_set
    gates: Dict[str, str] = {}
    for action in env_set:
        gates[action] = "env (frozen)"
    for action in posture_set:
        gates.setdefault(action, "posture")
    try:
        from core import prefs as _prefs
        pref_set = set(pref_gated_actions(user_id, home_dir))
        for action in pref_set:
            gates.setdefault(action, "pref")
        provider = _prefs.resolve(
            "approvals.provider", user_id, home_dir,
            env_value=provider, default=provider,
        )
    except Exception as e:
        logger.error(f"effective_approval_state: pref union skipped (non-fatal): {e}")
    return gates, provider


def _refreeze_approval_flags_for_tests() -> None:
    """TEST-ONLY: re-snapshot from the current env. Production never calls this."""
    global _FROZEN_APPROVAL_REQUIRED_TOOLS, _FROZEN_APPROVAL_PROVIDER
    _FROZEN_APPROVAL_REQUIRED_TOOLS = _snapshot_required_tools()
    _FROZEN_APPROVAL_PROVIDER = (os.getenv("APPROVAL_PROVIDER", "auto") or "auto").strip() or "auto"

# The RECOMMENDED set of mutating coding / self-evolution ops to gate behind approval.
# ⚠️ NOT auto-applied. `Controller.__init__` reads `APPROVAL_REQUIRED_TOOLS` (default
# empty → the hook is never registered) and defaults `APPROVAL_PROVIDER` to `auto`
# (AutoApprover = allow-all). So approval is INERT until an operator BOTH sets
# `APPROVAL_REQUIRED_TOOLS` (this set is a convenience default they can copy) AND wires a
# non-`auto` provider (`deny`, or the interactive `interactive_cli`). Gating without a
# real interactive provider only logs — it can't actually prompt a human. Permissions
# audit F5: the previous "the server sets APPROVAL_REQUIRED_TOOLS to this unless
# overridden" claim was aspirational (no wiring did that); see docs/CONFIGURATION.md.
DEFAULT_APPROVAL_REQUIRED_TOOLS = (
    "git_push", "github_open_pr", "github_merge_pr",
    "mcp_install", "tool_manage", "self_modify",
    "x402_request",  # outward-facing invoicing — recommend owner approval
    # NOTE: hf_deploy's `deploy` is deliberately NOT here. A blanket Controller
    # gate can't tell a FIRST publish (must be approved) from a redeploy of an
    # already-approved app (unattended within caps) — gating both would break the
    # "approved-app redeploy is unattended" contract. hf_deploy owns that
    # distinction itself via its deployed_apps registry (see tools/hf_deploy/tool.py).
)

# WS-6: the compute-tier action names auto-gated at AGENT_COMPUTE_POSTURE >= 2 (the
# self-maintenance tier). The persistent shell and every self_env verb require an
# owner approval decision before they run. UNIONed with DEFAULT_APPROVAL_REQUIRED_TOOLS
# in Controller.__init__ when posture >= 2.
POSTURE2_APPROVAL_REQUIRED_TOOLS = (
    "shell_run",
    "self_env_install_dep",
    "self_env_patch_source",
    "self_env_restart_service",
    "self_env_git_pull",
    # NOTE: hf_deploy's `deploy` is deliberately NOT here (see the note in
    # DEFAULT_APPROVAL_REQUIRED_TOOLS above). The tool gates FIRST publish through
    # its own registry-backed approver (resolving the SAME interactive-default
    # provider at posture>=2), and lets an already-approved app redeploy unattended.
)


def default_approval_required_tools() -> tuple:
    """The RECOMMENDED approval-gated action set (NOT auto-applied; opt-in via env).

    See :data:`DEFAULT_APPROVAL_REQUIRED_TOOLS` — nothing wires this by default; an
    operator opts in with ``APPROVAL_REQUIRED_TOOLS`` + a non-``auto`` ``APPROVAL_PROVIDER``.
    """
    return DEFAULT_APPROVAL_REQUIRED_TOOLS


class ApprovalProvider(ABC):
    """Decides whether a gated action may execute."""

    @abstractmethod
    async def request(self, action_name: str, params: Dict[str, Any], context: Any) -> bool:
        """Return True to allow the action, False to deny it.

        MUST be cancellation-safe: the caller bounds this with
        ``asyncio.wait_for(..., APPROVAL_TIMEOUT_SEC)``, which cancels this
        coroutine on timeout. Release any held resource (open prompt, network
        request, staged decision) in a ``finally`` / ``except asyncio.CancelledError``.
        """
        raise NotImplementedError


class AutoApprover(ApprovalProvider):
    """Always allow (default — current behaviour)."""

    async def request(self, action_name, params, context) -> bool:
        return True


class DenyByDefaultApprover(ApprovalProvider):
    """Always deny (safe default for an un-wired interactive provider)."""

    async def request(self, action_name, params, context) -> bool:
        return False


class AutoNotifyApprover(ApprovalProvider):
    """Allow — the act-and-report provider (013 T4), the generic analog of
    ``PAYMENT_APPROVAL_MODE=auto``.

    The audit event + post-hoc owner notification deliberately do NOT live here:
    the provider has no container/delivery rail. They live in the paired
    post-tool-call hook (``tools/controller/approval_queue.py::
    make_tool_auto_notify_hook``) so a gated action is reported exactly once,
    on its actual (non-error) result — mirroring the payment-side
    ``make_payment_auto_notify_hook`` architecture.
    """

    def __init__(self, user_id: Optional[str] = None, home_dir: Any = None):
        self._user_id = user_id
        self._home_dir = home_dir

    async def request(self, action_name, params, context) -> bool:
        return True


_PROVIDERS: Dict[str, type] = {
    "auto": AutoApprover,
    "auto_notify": AutoNotifyApprover,
    "deny": DenyByDefaultApprover,
}


# 013 T4: verbs that stay owner-queued even under AUTONOMY_MODE=autonomous —
# self-modification / host mutation is never act-and-report. Routed to the durable,
# remotely approvable `owner_queue` provider (Telegram /approve) rather than
# auto_notify. Verified against the real registered action names:
#   - self_env_* — tools/self_env/tool.py (posture-2 self-maintenance verbs);
#   - mcp_install — tools/controller/action_registration.py (_register_mcp_install_action).
# `self_modify` and `tool_manage` are NOT registered actions today — they are the
# same aspirational defense-in-depth tokens DEFAULT_APPROVAL_REQUIRED_TOOLS and the
# correspondent-gate high-impact set already carry, kept so a future action with
# either name can never silently land in the act-and-report lane.
_ALWAYS_GATED_VERBS = frozenset({
    "self_modify",
    "self_env_install_dep", "self_env_patch_source",
    "self_env_restart_service", "self_env_git_pull",
    "mcp_install", "tool_manage",
})


def autonomous_gating_lanes(gates: Dict[str, str]) -> tuple:
    """Split the effective gated set for act-and-report mode: ``(queued, reported)``.

    ``queued`` -> the durable `owner_queue` provider (always-gated
    self-modification verbs + owner ``approvals.require`` pref pins);
    ``reported`` -> `auto_notify` (allow + audit + post-hoc owner notify).
    Pure — takes the ``gates`` mapping :func:`effective_approval_state` returns
    (``{action: source}``, source in ``"env (frozen)"``/``"posture"``/``"pref"``).
    """
    queued = {a for a, src in gates.items() if a in _ALWAYS_GATED_VERBS or src == "pref"}
    return queued, set(gates) - queued


def register_approval_provider(name: str, cls: type) -> None:
    """Register a custom ApprovalProvider class under ``name`` (mirrors the registry seam)."""
    _PROVIDERS[name.lower()] = cls


def get_approval_provider(name: Optional[str], *, user_id: Optional[str] = None,
                          home_dir: Any = None) -> ApprovalProvider:
    """Resolve a provider instance by name; unknown name raises a clear error.

    owner-UX P2 T5: ``user_id``/``home_dir`` (the tenant context the approval
    ladder needs for its [a]lways-allow/[n]ever prefs bookkeeping) are passed
    through to the provider's constructor when it accepts them. A provider
    that doesn't (``AutoApprover``/``DenyByDefaultApprover``, or a legacy
    custom-registered class) is constructed with no args instead — never
    crash on an incompatible constructor.
    """
    key = (name or "auto").lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise ValueError(
            f"Unknown APPROVAL_PROVIDER '{name}' (known: {sorted(_PROVIDERS)})"
        )
    try:
        return cls(user_id=user_id, home_dir=home_dir)
    except TypeError:
        return cls()


def get_approval_provider_or_deny(name: Optional[str], *, user_id: Optional[str] = None,
                                  home_dir: Any = None) -> ApprovalProvider:
    """Resolve a provider by name; on an UNKNOWN name fall back to
    ``DenyByDefaultApprover`` (fail-CLOSED) with a loud error, so a misconfigured
    ``APPROVAL_PROVIDER`` never silently leaves gated tools ungated (H9). A ``None``
    name still resolves to the ``auto`` default (approval must be explicitly opted into
    via ``APPROVAL_REQUIRED_TOOLS`` before any of this runs)."""
    try:
        return get_approval_provider(name, user_id=user_id, home_dir=home_dir)
    except ValueError as e:
        logger.error(
            "approval provider misconfigured (%s) -> deny-by-default (fail-closed)", e
        )
        return DenyByDefaultApprover()


def _emit_approval_event(kind: str, action_name: str, context: Any, **fields: Any) -> None:
    """Emit a 019 approval wait-state feed event (fail-open, flag-gated).

    The approval layer holds no TelemetryManager, so this routes through the
    manager-free ``emit_feed_event`` seam; the event carries its own session_id
    (from the execution context) or is dropped.
    """
    try:
        from core.config_policy import AutonomyConfig
        if not AutonomyConfig.run_events_enabled():
            return
        session_id = getattr(context, "session_id", None)
        if not session_id:
            return
        from agents.task.telemetry.service import emit_feed_event
        from agents.task.telemetry.views import ApprovalResolvedEvent, AwaitingApprovalEvent
        if kind == "awaiting":
            event: Any = AwaitingApprovalEvent(
                session_id=session_id, action_name=action_name, **fields
            )
        else:
            event = ApprovalResolvedEvent(
                session_id=session_id, action_name=action_name, **fields
            )
        emit_feed_event(event)
    except Exception:
        logger.debug("approval run-event emit failed", exc_info=True)


def make_approval_hook(
    provider: ApprovalProvider,
    required_tools: Iterable[str],
    *,
    timeout: float = DEFAULT_APPROVAL_TIMEOUT_SEC,
) -> Callable:
    """Build a pre-tool-call hook gating ``required_tools`` through ``provider``.

    Returns the standard pre-hook signature ``(action_name, params, context) ->
    Optional[str]`` — a non-empty string DENIES with that reason; None allows.
    The hook is an **async** coroutine function (UP-04): it ``await``s the provider
    directly through the now-async hook pipeline, so a slow/interactive provider
    yields the loop instead of freezing it. The wait is bounded by
    ``asyncio.wait_for(..., timeout)``; timeout and error both DENY.
    """
    required = {t for t in (required_tools or []) if t}

    async def _hook(action_name, params, context):
        if action_name not in required:
            return None  # not gated -> allow
        # 019 P0: the wait is a first-class visible state — emit the span pair
        # (awaiting → resolved) around the provider wait so a blocked approval
        # never renders as a silent stall. Events carry action name + timing,
        # never raw params.
        import time as _time
        waited_from = _time.monotonic()
        decision = "error"  # overwritten on every exit path; CancelledError keeps it
        _emit_approval_event("awaiting", action_name, context, timeout_sec=timeout)
        try:
            try:
                approved = await asyncio.wait_for(
                    provider.request(action_name, params or {}, context),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                decision = "timeout"
                logger.error(f"approval.timeout action={action_name} after {timeout}s")
                return (f"approval denied (timeout) for '{action_name}'; owner can approve "
                        "via /pending or loosen via the approvals.require pref")
            except Exception as e:
                logger.error(
                    f"approval.error action={action_name} exc={type(e).__name__}: {e}"
                )
                return (f"approval denied (error) for '{action_name}'; owner can approve "
                        "via /pending or loosen via the approvals.require pref")
            decision = "approved" if approved else "denied"
            if not approved:
                return (f"approval denied for '{action_name}'; owner can approve via /pending "
                        "or loosen via the approvals.require pref")
            return None
        finally:
            _emit_approval_event(
                "resolved", action_name, context,
                decision=decision, waited_sec=_time.monotonic() - waited_from,
            )

    return _hook
