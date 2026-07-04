"""Self-evolution transparency loop (§7.1).

The agent has two pending→promote pipelines that let it evolve from experience:
the writable SELF-context doc (``core/self_context_writer.py``) and authored skills
(``agents/task/agent/skill_writer.py``). Both quarantine agent/background proposals
to ``.pending`` and require an owner promote. Historically the owner was **never told**
a proposal existed and — for self-context — had **no reachable approve surface** at all.

This module is the missing middle: one owner-facing aggregator over BOTH pipelines so
the owner can SEE what the agent proposed, and PROMOTE or REJECT it from one place
(the ``polyrob owner`` CLI + a Telegram command verb wire onto these functions), plus
a notification builder so the agent can proactively surface "I learned X — approve?".

Pure/dependency-injected: callers pass the resolved ``home_dir`` + ``instance_id`` and
an optional ``skill_manager`` so this stays unit-testable without the DI container. All
authorization (owner-only) is the caller's job — these functions do not gate on identity.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

KIND_SELF_CONTEXT = "self_context"
KIND_SKILL = "skill"


def _writer(home_dir: Path | str, instance_id: str):
    from core.self_context_writer import SelfContextWriter
    return SelfContextWriter(home_dir, instance_id=instance_id)


def _resolve_skill_manager(skill_manager):
    if skill_manager is not None:
        return skill_manager
    try:
        from agents.task.agent.skill_manager import get_skill_manager
        return get_skill_manager()
    except Exception as e:  # fail-open: skills simply won't be listed
        logger.debug("self_evolution: skill manager unavailable: %s", e)
        return None


def list_pending(user_id: str, *, home_dir: Path | str, instance_id: str,
                 skill_manager=None) -> List[dict]:
    """Aggregate a tenant's pending self-evolution proposals across both pipelines.

    Each item: ``{kind, id, preview, chars, path}`` — ``id`` is the tenant for a
    self-context draft (one per tenant) or the skill id for a skill draft.
    """
    items: List[dict] = []

    try:
        sc = _writer(home_dir, instance_id).list_pending(user_id)
        if sc:
            items.append({
                "kind": KIND_SELF_CONTEXT,
                "id": sc["user_id"],
                "preview": sc["preview"],
                "chars": sc["chars"],
                "path": sc["path"],
            })
    except Exception as e:
        logger.debug("self_evolution: self-context list failed: %s", e)

    mgr = _resolve_skill_manager(skill_manager)
    if mgr is not None and hasattr(mgr, "list_pending_skills"):
        try:
            for s in mgr.list_pending_skills(user_id=user_id):
                items.append({
                    "kind": KIND_SKILL,
                    "id": s["skill_id"],
                    "preview": s["preview"],
                    "chars": s["chars"],
                    "path": s["path"],
                })
        except Exception as e:
            logger.debug("self_evolution: skill list failed: %s", e)

    return items


def promote(kind: str, item_id: str, *, user_id: str, home_dir: Path | str,
            instance_id: str, skill_manager=None) -> Tuple[bool, str]:
    """Promote a pending proposal to active. Returns ``(ok, message)``."""
    if kind == KIND_SELF_CONTEXT:
        res = _writer(home_dir, instance_id).promote(user_id=user_id)
        return bool(res.ok and not res.pending), (
            "self-context promoted (active next session)" if res.ok
            else "; ".join(res.errors) or "promote failed")
    if kind == KIND_SKILL:
        mgr = _resolve_skill_manager(skill_manager)
        if mgr is None:
            return False, "skill manager unavailable"
        res = mgr.promote_pending_skill(item_id, user_id=user_id)
        return bool(getattr(res, "ok", False) and not getattr(res, "pending", False)), (
            f"skill '{item_id}' promoted (active)" if getattr(res, "ok", False)
            else "; ".join(getattr(res, "errors", []) or []) or "promote failed")
    return False, f"unknown pending kind: {kind!r}"


def reject(kind: str, item_id: str, *, user_id: str, home_dir: Path | str,
           instance_id: str, skill_manager=None) -> Tuple[bool, str]:
    """Reject (archive-then-discard) a pending proposal. Returns ``(ok, message)``."""
    if kind == KIND_SELF_CONTEXT:
        res = _writer(home_dir, instance_id).reject(user_id=user_id)
        return bool(res.ok), ("self-context draft rejected (archived)" if res.ok
                              else "; ".join(res.errors) or "reject failed")
    if kind == KIND_SKILL:
        mgr = _resolve_skill_manager(skill_manager)
        if mgr is None:
            return False, "skill manager unavailable"
        ok = bool(mgr.reject_pending_skill(item_id, user_id=user_id))
        return ok, (f"skill '{item_id}' rejected (archived)" if ok
                    else f"no pending skill '{item_id}'")
    return False, f"unknown pending kind: {kind!r}"


def build_pending_notification(items: List[dict]) -> Optional[str]:
    """Turn a pending set into one proactive owner message (or None if empty).

    Kept terse: the owner scans it on a phone. Names each proposal + how to act.
    """
    if not items:
        return None
    lines = [f"🧠 I've proposed {len(items)} change(s) to how I work — approve to make them stick:"]
    for it in items:
        label = "identity note" if it["kind"] == KIND_SELF_CONTEXT else f"skill '{it['id']}'"
        preview = (it.get("preview") or "").strip()
        if len(preview) > 140:
            preview = preview[:137] + "…"
        lines.append(f"• {label} — {preview}")
    lines.append("Reply \"approve\" / \"reject\" (tell me which), "
                 "or run `polyrob owner pending` to review + `owner promote/reject`.")
    return "\n".join(lines)


async def push_owner_message(container, text: Optional[str]) -> bool:
    """Best-effort proactive push to the owner's Telegram (fail-open).

    Reuses the same sink + owner-chat resolution as ``cron/delivery`` so a
    self-evolution notification rides the exact rail an out-of-band cron report does.
    """
    if not text or container is None:
        return False
    try:
        sink = None
        if hasattr(container, "get_service"):
            sink = container.get_service("telegram_sink") or container.get_service("message_router")
        from core.instance import resolve_owner_telegram_id
        chat_id = resolve_owner_telegram_id()
        if sink is None or not chat_id:
            return False
        send = getattr(sink, "send_message", None)
        if send is None:
            return False
        res = send(str(chat_id), text)
        if hasattr(res, "__await__"):
            res = await res
        return bool(res)
    except Exception as e:  # never let a notification failure break a write
        logger.debug("self_evolution: owner notify failed (fail-open): %s", e)
        return False


async def maybe_notify_owner_pending(container, user_id: str, *, home_dir: Path | str,
                                     instance_id: str, skill_manager=None) -> bool:
    """Notify the owner of this tenant's pending proposals if transparency is enabled.

    Gated ``SELF_EVOLUTION_TRANSPARENCY`` (default OFF on server, ON under
    POLYROB_LOCAL). No-op + fail-open otherwise.
    """
    try:
        from agents.task.constants import AutonomyConfig
        if not AutonomyConfig.self_evolution_transparency():
            return False
    except Exception:
        return False
    items = list_pending(user_id, home_dir=home_dir, instance_id=instance_id,
                         skill_manager=skill_manager)
    msg = build_pending_notification(items)
    return await push_owner_message(container, msg)


__all__ = [
    "KIND_SELF_CONTEXT", "KIND_SKILL",
    "list_pending", "promote", "reject", "build_pending_notification",
    "push_owner_message", "maybe_notify_owner_pending",
]
