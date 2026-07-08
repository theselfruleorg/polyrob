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
KIND_OWNER = "owner_doc"


def _writer(home_dir: Path | str, instance_id: str):
    from core.self_context_writer import SelfContextWriter
    return SelfContextWriter(home_dir, instance_id=instance_id)


def _owner_writer(home_dir: Path | str, instance_id: str):
    from core.owner_doc_writer import OwnerDocWriter
    return OwnerDocWriter(home_dir, instance_id=instance_id)


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

    try:
        od = _owner_writer(home_dir, instance_id).list_pending(user_id)
        if od:
            items.append({
                "kind": KIND_OWNER,
                "id": od["user_id"],
                "preview": od["preview"],
                "chars": od["chars"],
                "path": od["path"],
            })
    except Exception as e:
        logger.debug("self_evolution: owner-doc list failed: %s", e)

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


def _self_mod_ev(kind: str, action: str, item_id: str, user_id: str, ok: bool) -> None:
    """T4-06: record the owner's promote/reject decision as a first-class
    self_modification event on the durable log. Fail-open."""
    if not ok:
        return
    try:
        from agents.task.telemetry.self_events import emit_self_modification
        emit_self_modification(kind=kind, action=action, item_id=item_id,
                               user_id=user_id or "", pending=False,
                               created_by="owner", source="owner_review", ok=ok)
    except Exception as e:
        logger.debug("self_evolution: event emit skipped: %s", e)


def promote(kind: str, item_id: str, *, user_id: str, home_dir: Path | str,
            instance_id: str, skill_manager=None) -> Tuple[bool, str]:
    """Promote a pending proposal to active. Returns ``(ok, message)``."""
    if kind == KIND_SELF_CONTEXT:
        res = _writer(home_dir, instance_id).promote(user_id=user_id)
        ok = bool(res.ok and not res.pending)
        _self_mod_ev(kind, "promote", item_id, user_id, ok)
        return ok, (
            "self-context promoted (active next session)" if res.ok
            else "; ".join(res.errors) or "promote failed")
    if kind == KIND_OWNER:
        res = _owner_writer(home_dir, instance_id).promote(user_id=user_id)
        ok = bool(res.ok and not res.pending)
        _self_mod_ev(kind, "promote", item_id, user_id, ok)
        return ok, (
            "owner-facts doc promoted (active next session)" if res.ok
            else "; ".join(res.errors) or "promote failed")
    if kind == KIND_SKILL:
        mgr = _resolve_skill_manager(skill_manager)
        if mgr is None:
            return False, "skill manager unavailable"
        res = mgr.promote_pending_skill(item_id, user_id=user_id)
        ok = bool(getattr(res, "ok", False) and not getattr(res, "pending", False))
        _self_mod_ev(kind, "promote", item_id, user_id, ok)
        return ok, (
            f"skill '{item_id}' promoted (active)" if getattr(res, "ok", False)
            else "; ".join(getattr(res, "errors", []) or []) or "promote failed")
    return False, f"unknown pending kind: {kind!r}"


def reject(kind: str, item_id: str, *, user_id: str, home_dir: Path | str,
           instance_id: str, skill_manager=None) -> Tuple[bool, str]:
    """Reject (archive-then-discard) a pending proposal. Returns ``(ok, message)``."""
    if kind == KIND_SELF_CONTEXT:
        res = _writer(home_dir, instance_id).reject(user_id=user_id)
        _self_mod_ev(kind, "reject", item_id, user_id, bool(res.ok))
        return bool(res.ok), ("self-context draft rejected (archived)" if res.ok
                              else "; ".join(res.errors) or "reject failed")
    if kind == KIND_OWNER:
        res = _owner_writer(home_dir, instance_id).reject(user_id=user_id)
        _self_mod_ev(kind, "reject", item_id, user_id, bool(res.ok))
        return bool(res.ok), ("owner-facts draft rejected (archived)" if res.ok
                              else "; ".join(res.errors) or "reject failed")
    if kind == KIND_SKILL:
        mgr = _resolve_skill_manager(skill_manager)
        if mgr is None:
            return False, "skill manager unavailable"
        ok = bool(mgr.reject_pending_skill(item_id, user_id=user_id))
        _self_mod_ev(kind, "reject", item_id, user_id, ok)
        return ok, (f"skill '{item_id}' rejected (archived)" if ok
                    else f"no pending skill '{item_id}'")
    return False, f"unknown pending kind: {kind!r}"


def show(kind: str, item_id: str, *, user_id: str, home_dir: Path | str,
         instance_id: str, skill_manager=None, cap_chars: int = 40000) -> Tuple[bool, str]:
    """Full-body review of ONE pending proposal (T3-09).

    The owner previously decided from a ~160-char preview; this returns the whole
    quarantined body (capped) so promote/reject is an informed decision.
    Returns ``(ok, body-or-error-message)``.
    """
    try:
        items = list_pending(user_id, home_dir=home_dir, instance_id=instance_id,
                             skill_manager=skill_manager)
        match = next((it for it in items
                      if it["kind"] == kind and str(it["id"]) == str(item_id)), None)
        if match is None:
            return False, f"no pending {kind} '{item_id}' for tenant {user_id}"
        body = Path(match["path"]).read_text(encoding="utf-8")
        if len(body) > cap_chars:
            body = body[:cap_chars] + f"\n[... truncated at {cap_chars} chars]"
        return True, body
    except Exception as e:
        logger.debug("self_evolution: show failed: %s", e)
        return False, f"could not read pending {kind} '{item_id}': {e}"


def build_pending_notification(items: List[dict]) -> Optional[str]:
    """Turn a pending set into one proactive owner message (or None if empty).

    Kept terse: the owner scans it on a phone. Names each proposal + how to act.
    """
    if not items:
        return None
    lines = [f"🧠 I've proposed {len(items)} change(s) to how I work — approve to make them stick:"]
    for it in items:
        if it["kind"] == KIND_SELF_CONTEXT:
            label = "identity note"
        elif it["kind"] == KIND_OWNER:
            label = "owner-facts note"
        else:
            label = f"skill '{it['id']}'"
        preview = (it.get("preview") or "").strip()
        if len(preview) > 140:
            preview = preview[:137] + "…"
        lines.append(f"• {label} — {preview}")
    lines.append("Reply \"approve\" / \"reject\" (tell me which), "
                 "or run `polyrob owner pending` to review + `owner promote/reject`.")
    return "\n".join(lines)


def _record_owner_notice(text: str) -> None:
    """T4-04 fallback: persist an owner-facing notice to the durable event log when a
    live push couldn't be delivered, so a REPL/local owner (no telegram daemon) still
    sees it via `polyrob telemetry` and the message is never silently lost. Owner-scoped,
    fail-open."""
    try:
        from agents.task.telemetry.event_log import get_event_log
        from core.instance import resolve_owner_principal
        owner = resolve_owner_principal() or ""
        get_event_log().record("owner_notice", user_id=str(owner),
                                source="self_evolution", text=str(text)[:2000])
    except Exception:
        pass


async def push_owner_message(container, text: Optional[str]) -> bool:
    """Best-effort proactive push to the owner's Telegram (fail-open).

    Reuses the same sink + owner-chat resolution as ``cron/delivery`` so a
    self-evolution notification rides the exact rail an out-of-band cron report does.

    T4-04: the previous version returned False (silently) whenever no telegram sink /
    owner chat was registered — the exact case for a plain REPL/local owner, where the
    flag defaults ON but no sink exists, so every escalation/self-evolution push
    vanished. Now a push that can't be delivered live is persisted as a durable
    ``owner_notice`` event instead, so the owner is never left in the dark.
    """
    if not text or container is None:
        return False
    sent = False
    try:
        sink = None
        if hasattr(container, "get_service"):
            sink = container.get_service("telegram_sink") or container.get_service("message_router")
        from core.instance import resolve_owner_telegram_id
        chat_id = resolve_owner_telegram_id()
        if sink is not None and chat_id:
            send = getattr(sink, "send_message", None)
            if send is not None:
                res = send(str(chat_id), text)
                if hasattr(res, "__await__"):
                    res = await res
                sent = bool(res)
    except Exception as e:  # never let a notification failure break a write
        logger.debug("self_evolution: owner notify failed (fail-open): %s", e)
        sent = False
    if not sent:
        _record_owner_notice(text)
    return sent


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
    "KIND_SELF_CONTEXT", "KIND_SKILL", "KIND_OWNER",
    "list_pending", "promote", "reject", "show", "build_pending_notification",
    "push_owner_message", "maybe_notify_owner_pending",
]
