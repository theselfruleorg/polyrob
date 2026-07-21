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

import hashlib
import logging
from pathlib import Path
from core.config_policy import AutonomyConfig
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

KIND_SELF_CONTEXT = "self_context"
KIND_SKILL = "skill"
KIND_OWNER = "owner_doc"
KIND_CONTRACT = "contract"
KIND_PREF_CHANGE = "pref_change"

# Short display label per pending-proposal kind (owner-UX P2-4 final review,
# item 4). Every "/pending"-style presentation surface — the REPL `/pending`
# handler (``cli/ui/commands/handlers.py``) and `polyrob owner pending`
# (``cli/commands/owner.py``) — used to hardcode its OWN two-kind map
# (self_context vs. everything-else labeled "skill"), so an ``owner_doc``/
# ``contract``/``pref_change`` proposal displayed as a generic "[skill]" —
# wrong, and confusing when deciding whether to approve. This is the same
# five-kind set :func:`build_pending_notification` below already labels
# correctly; every surface now reads from here instead of drifting.
PENDING_KIND_LABELS = {
    KIND_SELF_CONTEXT: "identity",
    KIND_OWNER: "owner facts",
    KIND_CONTRACT: "contract",
    KIND_PREF_CHANGE: "pref change",
    KIND_SKILL: "skill",
}


def pending_kind_label(kind: str) -> str:
    """Short display label for a self-evolution pending-proposal ``kind``.

    Falls back to the raw ``kind`` string for anything outside the five known
    kinds (e.g. ``polyrob owner pending``'s additional ``tool_approval``/
    ``correspondent`` items, which ride a different, non-self-evolution
    pipeline) — never mislabels an unrecognized kind as "skill".
    """
    return PENDING_KIND_LABELS.get(kind, kind)


def _writer(home_dir: Path | str, instance_id: str):
    from core.self_context_writer import SelfContextWriter
    return SelfContextWriter(home_dir, instance_id=instance_id)


def _owner_writer(home_dir: Path | str, instance_id: str):
    from core.owner_doc_writer import OwnerDocWriter
    return OwnerDocWriter(home_dir, instance_id=instance_id)


def _contract_writer(home_dir: Path | str, instance_id: str):
    from core.contract_writer import ContractWriter
    return ContractWriter(home_dir, instance_id=instance_id)


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

    try:
        cd = _contract_writer(home_dir, instance_id).list_pending(user_id)
        if cd:
            items.append({
                "kind": KIND_CONTRACT,
                "id": cd["user_id"],
                "preview": cd["preview"],
                "chars": cd["chars"],
                "path": cd["path"],
            })
    except Exception as e:
        logger.debug("self_evolution: contract list failed: %s", e)

    try:
        from core.prefs import list_pending_pref_changes
        for pc in list_pending_pref_changes(user_id, home_dir, instance_id):
            items.append({
                "kind": KIND_PREF_CHANGE,
                "id": pc["id"],
                "preview": pc["preview"],
                "chars": pc["chars"],
                "path": pc["path"],
            })
    except Exception as e:
        logger.debug("self_evolution: pref-change list failed: %s", e)

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
    # 019 #1: an owner action changes the pending set — forget the notified
    # fingerprint so the next (possibly same-id) proposal notifies again.
    _clear_notified_fingerprint(user_id, home_dir=home_dir, instance_id=instance_id)
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
    if kind == KIND_CONTRACT:
        res = _contract_writer(home_dir, instance_id).promote(user_id=user_id)
        ok = bool(res.ok and not res.pending)
        _self_mod_ev(kind, "promote", item_id, user_id, ok)
        return ok, (
            "operating contract promoted (active next session)" if res.ok
            else "; ".join(res.errors) or "promote failed")
    if kind == KIND_PREF_CHANGE:
        from core.prefs import promote_pref_change
        ok, msg = promote_pref_change(item_id, user_id=user_id, home_dir=home_dir,
                                      instance_id=instance_id)
        _self_mod_ev(kind, "promote", item_id, user_id, ok)
        return ok, msg
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
    # 019 #1: see promote() — owner action resets the notification batch.
    _clear_notified_fingerprint(user_id, home_dir=home_dir, instance_id=instance_id)
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
    if kind == KIND_CONTRACT:
        res = _contract_writer(home_dir, instance_id).reject(user_id=user_id)
        _self_mod_ev(kind, "reject", item_id, user_id, bool(res.ok))
        return bool(res.ok), ("operating contract draft rejected (archived)" if res.ok
                              else "; ".join(res.errors) or "reject failed")
    if kind == KIND_PREF_CHANGE:
        from core.prefs import reject_pref_change
        ok, msg = reject_pref_change(item_id, user_id=user_id, home_dir=home_dir,
                                     instance_id=instance_id)
        _self_mod_ev(kind, "reject", item_id, user_id, ok)
        return ok, msg
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
        elif it["kind"] == KIND_CONTRACT:
            label = "operating contract"
        elif it["kind"] == KIND_PREF_CHANGE:
            label = f"preference change '{it['id']}'"
        else:
            label = f"skill '{it['id']}'"
        preview = (it.get("preview") or "").strip()
        if len(preview) > 140:
            preview = preview[:137] + "…"
        lines.append(f"• {label} — {preview}")
    lines.append("Reply \"approve\" / \"reject\" (tell me which), "
                 "or run `polyrob owner pending` to review + `owner promote/reject`.")
    return "\n".join(lines)


# --- 019 #1: pending-notification batching -----------------------------------
#
# ``maybe_notify_owner_pending`` used to fire a proactive owner delivery on
# EVERY pending write — on 2026-07-18, 29 of the 30 shared daily delivery
# slots were burned by it, starving the daily digest (proposal 019). The fix:
# fingerprint the pending set (stable hash of the sorted ``kind:id`` pairs)
# and skip the push when the set is UNCHANGED since the last notification. A
# genuinely NEW pending item changes the fingerprint and still notifies
# promptly; an owner promote/reject clears the fingerprint so a re-proposal
# under the same id notifies again. The fingerprint is persisted durably next
# to the pending stores themselves (the tenant's identity tier root,
# ``core.instance.self_tier_root``); the module-level dict is only the
# fallback for tenant ids the identity store refuses — a process restart then
# re-notifies once, which is fine.

_NOTIFY_FINGERPRINT_FILE = ".last_notified_pending"
_notify_fingerprints: dict = {}  # (home_dir, instance_id, user_id) -> fingerprint

# Rail outcomes after which the pending set counts as "notified": the owner
# either got the push live (``sent``), already received the identical text in
# the dedup window (``deduped``), or holds a durable record of it
# (``fallback``/``capped`` owner_notice, ``quiet_held`` held_text).
# ``rate_limited`` is deliberately NOT here — nothing durable was written, so
# the next pending write may retry.
_NOTIFIED_OUTCOMES = ("sent", "deduped", "fallback", "capped", "quiet_held")


def _pending_fingerprint(items: List[dict]) -> str:
    """Stable fingerprint of a pending SET (sorted ``kind:id`` pairs).

    Content-independent by design: iterating on the SAME pending draft must not
    re-notify (that is exactly the spam class 019 root-caused), while a new
    distinct pending item always changes the set."""
    keys = sorted(f"{it.get('kind', '')}:{it.get('id', '')}" for it in items)
    return hashlib.sha256("\n".join(keys).encode("utf-8", "replace")).hexdigest()[:16]


def _fingerprint_path(user_id: str, home_dir: Path | str,
                      instance_id: str) -> Optional[Path]:
    try:
        from core.instance import is_safe_tenant_id, self_tier_root
        if not is_safe_tenant_id(user_id):
            return None
        return self_tier_root(home_dir, user_id, instance_id) / _NOTIFY_FINGERPRINT_FILE
    except Exception:
        return None


def _load_notified_fingerprint(user_id: str, *, home_dir: Path | str,
                               instance_id: str) -> Optional[str]:
    path = _fingerprint_path(user_id, home_dir, instance_id)
    if path is not None:
        try:
            if path.exists():
                return path.read_text(encoding="utf-8").strip() or None
        except Exception as e:
            logger.debug("self_evolution: fingerprint read failed: %s", e)
    return _notify_fingerprints.get((str(home_dir), str(instance_id), str(user_id)))


def _save_notified_fingerprint(user_id: str, fingerprint: str, *,
                               home_dir: Path | str, instance_id: str) -> None:
    _notify_fingerprints[(str(home_dir), str(instance_id), str(user_id))] = fingerprint
    path = _fingerprint_path(user_id, home_dir, instance_id)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(fingerprint, encoding="utf-8")
        except Exception as e:
            logger.debug("self_evolution: fingerprint write failed: %s", e)


def _clear_notified_fingerprint(user_id: str, *, home_dir: Path | str,
                                instance_id: str) -> None:
    """Forget the last-notified pending set (owner acted / set went empty), so
    the NEXT pending proposal — even one recreated under the same id, e.g. a
    fresh self-context draft after a promote — notifies again. Fail-open."""
    _notify_fingerprints.pop((str(home_dir), str(instance_id), str(user_id)), None)
    path = _fingerprint_path(user_id, home_dir, instance_id)
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except Exception as e:
            logger.debug("self_evolution: fingerprint clear failed: %s", e)


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


async def _push_owner_message_outcome(container, text: Optional[str],
                                      attachments: Optional[list] = None,
                                      priority: Optional[str] = None) -> Optional[str]:
    """Rail outcome of a proactive owner push, or ``None`` when nothing entered
    the rail (empty text / no container / resolver crash). Shared by
    :func:`push_owner_message` (bool facade) and
    :func:`maybe_notify_owner_pending` (which needs the outcome to decide
    whether the pending set counts as notified — 019 #1)."""
    if not text or container is None:
        return None
    # §3.2 (intelligence-stack finalization): all user-bound sends ride the ONE
    # delivery rail — content-hash dedup, per-tenant rate limit + daily cap, and
    # the durable owner_notice fallback now live THERE, shared with the agent's
    # autonomous send_message and cron delivery.
    try:
        from core.instance import resolve_owner_principal
        from core.surfaces.user_delivery import deliver_user_message
        owner = ""
        try:
            owner = str(resolve_owner_principal() or "")
        except Exception:
            owner = ""
        return await deliver_user_message(
            container, owner, str(text), source="self_evolution",
            attachments=attachments, priority=priority)
    except Exception as e:  # never let a notification failure break a write
        logger.debug("self_evolution: owner notify failed (fail-open): %s", e)
        return None


async def push_owner_message(container, text: Optional[str],
                             attachments: Optional[list] = None,
                             priority: Optional[str] = None) -> bool:
    """Best-effort proactive push to the owner's Telegram (fail-open).

    Reuses the same sink + owner-chat resolution as ``cron/delivery`` so a
    self-evolution notification rides the exact rail an out-of-band cron report does.

    T4-04: the previous version returned False (silently) whenever no telegram sink /
    owner chat was registered — the exact case for a plain REPL/local owner, where the
    flag defaults ON but no sink exists, so every escalation/self-evolution push
    vanished. Now a push that can't be delivered live is persisted as a durable
    ``owner_notice`` event instead, so the owner is never left in the dark.

    ``attachments`` (QW-1): pre-validated media entries riding the same rail
    (see ``core.surfaces.attachments``); omitted => byte-identical legacy push.

    ``priority`` (2026-07-20): pass ``"low"`` for chatter that must never
    out-compete a completion, a digest, or a halt notice for the daily cap —
    see ``core.surfaces.user_delivery``. Omitted => ``normal``, unchanged.
    """
    return (await _push_owner_message_outcome(
        container, text, attachments=attachments, priority=priority)) == "sent"


async def maybe_notify_owner_pending(container, user_id: str, *, home_dir: Path | str,
                                     instance_id: str, skill_manager=None) -> bool:
    """Notify the owner of this tenant's pending proposals if transparency is enabled.

    Gated ``SELF_EVOLUTION_TRANSPARENCY`` (default OFF on server, ON under
    POLYROB_LOCAL). No-op + fail-open otherwise.

    019 #1 — batched: skips the push when the pending set is UNCHANGED since the
    last notification (fingerprint of sorted ``kind:id`` pairs, persisted under
    the tenant's identity tier root), so a goal that repeatedly touches the same
    pending draft can no longer burn the shared daily delivery cap. A genuinely
    new pending item still notifies promptly.
    """
    try:
        if not AutonomyConfig.self_evolution_transparency():
            return False
    except Exception:
        return False
    items = list_pending(user_id, home_dir=home_dir, instance_id=instance_id,
                         skill_manager=skill_manager)
    if not items:
        # Set went empty (everything promoted/rejected/expired): reset the
        # batch so the next pending item always notifies.
        _clear_notified_fingerprint(user_id, home_dir=home_dir,
                                    instance_id=instance_id)
        return False
    fingerprint = _pending_fingerprint(items)
    if fingerprint == _load_notified_fingerprint(user_id, home_dir=home_dir,
                                                 instance_id=instance_id):
        logger.debug("self_evolution: pending set unchanged since last "
                     "notification; skipping owner notify")
        return False
    msg = build_pending_notification(items)
    outcome = await _push_owner_message_outcome(container, msg)
    if outcome in _NOTIFIED_OUTCOMES:
        _save_notified_fingerprint(user_id, fingerprint, home_dir=home_dir,
                                   instance_id=instance_id)
    return outcome == "sent"


__all__ = [
    "KIND_SELF_CONTEXT", "KIND_SKILL", "KIND_OWNER", "KIND_CONTRACT", "KIND_PREF_CHANGE",
    "PENDING_KIND_LABELS", "pending_kind_label",
    "list_pending", "promote", "reject", "show", "build_pending_notification",
    "push_owner_message", "maybe_notify_owner_pending",
]
