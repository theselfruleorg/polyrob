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
        from agents.task.constants import compute_posture
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
    return required, provider


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


_PROVIDERS: Dict[str, type] = {
    "auto": AutoApprover,
    "deny": DenyByDefaultApprover,
}


def register_approval_provider(name: str, cls: type) -> None:
    """Register a custom ApprovalProvider class under ``name`` (mirrors the registry seam)."""
    _PROVIDERS[name.lower()] = cls


def get_approval_provider(name: Optional[str]) -> ApprovalProvider:
    """Resolve a provider instance by name; unknown name raises a clear error."""
    key = (name or "auto").lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise ValueError(
            f"Unknown APPROVAL_PROVIDER '{name}' (known: {sorted(_PROVIDERS)})"
        )
    return cls()


def get_approval_provider_or_deny(name: Optional[str]) -> ApprovalProvider:
    """Resolve a provider by name; on an UNKNOWN name fall back to
    ``DenyByDefaultApprover`` (fail-CLOSED) with a loud error, so a misconfigured
    ``APPROVAL_PROVIDER`` never silently leaves gated tools ungated (H9). A ``None``
    name still resolves to the ``auto`` default (approval must be explicitly opted into
    via ``APPROVAL_REQUIRED_TOOLS`` before any of this runs)."""
    try:
        return get_approval_provider(name)
    except ValueError as e:
        logger.error(
            "approval provider misconfigured (%s) -> deny-by-default (fail-closed)", e
        )
        return DenyByDefaultApprover()


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
        try:
            approved = await asyncio.wait_for(
                provider.request(action_name, params or {}, context),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error(f"approval.timeout action={action_name} after {timeout}s")
            return f"approval denied (timeout) for '{action_name}'"
        except Exception as e:
            logger.error(
                f"approval.error action={action_name} exc={type(e).__name__}: {e}"
            )
            return f"approval denied (error) for '{action_name}'"
        if not approved:
            return f"approval denied for '{action_name}'"
        return None

    return _hook
