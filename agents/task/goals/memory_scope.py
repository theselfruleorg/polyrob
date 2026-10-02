"""The goal side of the memory-scope regime (025): label, clamp, promotion.

The storage and the read predicates live in ``modules/memory`` (the provider is
the one enforcement point). This module answers the three questions only the
goal board can:

* **which scope** — ``goal:<root>``, where root is the topmost ``kind='goal'``
  ancestor via ``parent_id`` (objectives are not scopes). A DAG therefore shares
  one working memory, and attempt N+1 of a goal recalls attempt N's findings;
* **which regime** — ``payload.memory_regime`` (already clamped at
  ``goal_create``) > ``AUTONOMY_MEMORY_REGIME``, then RE-clamped here against the
  root's regime: agent input is never the authority, and a quarantined DAG never
  gains a shared-writing child;
* **when to promote** — only when the ROOT goal completes and the run is
  ``verified`` (policy :data:`modules.memory.scope.PROMOTE_POLICY`). A child's
  completion never releases the DAG's quarantine.

Inert unless ``MEMORY_SCOPES_ENABLED``. Fail-open: an error means "no scope" on
the read side of this module and "no promotion" on the write side — rows then
stay quarantined until the curator's retention purge, never leak.
"""
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_DEPTH = 32


def root_goal(board, goal) -> Any:
    """The topmost ``kind='goal'`` ancestor of *goal* (itself when it has none)."""
    cur, seen = goal, {getattr(goal, "id", None)}
    for _ in range(_MAX_DEPTH):
        pid = getattr(cur, "parent_id", None)
        if not pid or pid in seen:
            break
        try:
            parent = board.get(pid)
        except Exception:
            parent = None
        if parent is None or getattr(parent, "kind", "goal") != "goal":
            break
        seen.add(pid)
        cur = parent
    return cur


def goal_regime(board, goal) -> str:
    """The effective regime for one run of *goal* (see module doc)."""
    from modules.memory.scope import clamp_regime, default_regime, normalize_regime
    own = normalize_regime((getattr(goal, "payload", None) or {}).get("memory_regime"))
    regime = own or default_regime()
    root = root_goal(board, goal)
    if root is not goal:
        root_own = normalize_regime((getattr(root, "payload", None) or {}).get("memory_regime"))
        regime = clamp_regime(root_own or default_regime(), regime)
    return regime


def goal_request_fields(board, goal) -> dict:
    """The ``memory_scope`` / ``memory_regime`` keys for this goal's run request,
    or ``{}`` (flag off, or the run resolves to ``shared``)."""
    try:
        from modules.memory.scope import build_spec, goal_label, scopes_enabled
        if not scopes_enabled():
            return {}
        spec = build_spec(goal_label(root_goal(board, goal).id), goal_regime(board, goal))
        if spec is None:
            return {}
        return {"memory_scope": spec.label, "memory_regime": spec.regime}
    except Exception:
        logger.warning("goal memory scope resolution failed for %s — running shared",
                       getattr(goal, "id", "?"), exc_info=True)
        return {}


def promote_after_success(board, goal, *, verified: str) -> int:
    """Promote the scope of a ROOT goal that just completed. Returns the rows
    promoted (0 when nothing applies). Emits the ``memory_promoted`` goal event
    and the ``memory_promoted`` telemetry row only when N > 0."""
    try:
        from modules.memory.scope import PROMOTE_POLICY, goal_label, scopes_enabled
        if not scopes_enabled():
            return 0
        if PROMOTE_POLICY == "verified" and verified != "verified":
            return 0
        if root_goal(board, goal) is not goal:
            return 0  # a child's completion never releases the DAG's quarantine
        from modules.memory.registry import get_memory_registry
        provider = get_memory_registry().active()
        if provider is None or not hasattr(provider, "promote_scope"):
            return 0
        label = goal_label(goal.id)
        n = int(provider.promote_scope(goal.user_id, label) or 0)
    except Exception:
        logger.warning("memory scope promotion failed for %s — rows stay quarantined",
                       getattr(goal, "id", "?"), exc_info=True)
        return 0
    if n > 0:
        try:
            event = getattr(board, "_event", None)
            if callable(event):
                event(goal.id, "memory_promoted", {"memory_scope": label, "rows": n})
        except Exception:
            logger.debug("memory_promoted goal event skipped", exc_info=True)
        try:
            from core.event_log import event_log_enabled, get_event_log
            if event_log_enabled():
                get_event_log().record(
                    "memory_promoted", user_id=goal.user_id, source="goal",
                    goal_id=goal.id, memory_scope=label, count=n,
                    reason=f"{n} finding(s) promoted to shared recall",
                    preview=str(getattr(goal, "title", ""))[:120])
        except Exception:
            logger.debug("memory_promoted telemetry skipped", exc_info=True)
    return n


def promotion_line(n: int) -> Optional[str]:
    """The one line the owner's completion notice gains (never a separate push)."""
    if n <= 0:
        return None
    return f"Promoted {n} memor{'y' if n == 1 else 'ies'} to shared recall."
