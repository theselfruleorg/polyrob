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
import json
import logging
import re
from pathlib import Path
from core.config_policy import AutonomyConfig
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

KIND_SELF_CONTEXT = "self_context"
KIND_SKILL = "skill"
KIND_OWNER = "owner_doc"
KIND_CONTRACT = "contract"
KIND_PREF_CHANGE = "pref_change"
#: 043 A27: a curated note quarantined by a forged/autonomous turn
#: (``status='pending'`` in the ``curated_memory`` store). Historically
#: visible and promotable from no seat — ``promote`` returned "unknown
#: kind". This kind routes a promote/reject to ``note_update(status=…)``.
KIND_CURATED_NOTE = "curated_note"

#: Delivery lane for the pending-approval push (035 P0-2). Registered in
#: ``core.surfaces.user_delivery._CRITICAL_SOURCES`` and deliberately absent
#: from ``_LIFECYCLE_SOURCES``.
NOTIFY_SOURCE = "pending_approval"

# Short display label per pending-proposal kind (owner-UX P2-4 final review,
# item 4). Every "/pending"-style presentation surface — the REPL `/pending`
# handler (``cli/ui/commands/handlers.py``) and `polyrob owner pending`
# (``cli/commands/owner.py``) — used to hardcode its OWN two-kind map
# (self_context vs. everything-else labeled "skill"), so an ``owner_doc``/
# ``contract``/``pref_change`` proposal displayed as a generic "[skill]" —
# wrong, and confusing when deciding whether to approve. This is the same
# six-kind set :func:`build_pending_notification` below already labels
# correctly; every surface now reads from here instead of drifting.
PENDING_KIND_LABELS = {
    KIND_SELF_CONTEXT: "identity",
    KIND_OWNER: "owner facts",
    KIND_CONTRACT: "contract",
    KIND_PREF_CHANGE: "pref change",
    KIND_SKILL: "skill",
    KIND_CURATED_NOTE: "note",
}


#: The tappable-alias prefix for a self-evolution pending item (2026-09-15).
#:
#: Telegram auto-links ONE `/word` token of ``[A-Za-z0-9_]`` and sends the whole
#: token on tap. A proposal id is prose (``dm-conversation``,
#: ``treasury-track-record-post``): it carries its own separators, so it cannot
#: be folded into a single token and read back unambiguously, and the long ones
#: overflow the token length a client will link. So every pending item also gets
#: a SHORT opaque alias — the same trick the tool-approval lane already uses with
#: ``tap-<hex>`` — and the owner taps ``/approve_p_<hex>`` instead of copying an
#: id off a phone.
PENDING_ALIAS_PREFIX = "p-"


def pending_tap_alias(kind: str, item_id: str) -> str:
    """The short, stable, tappable alias for one pending proposal.

    Pure and deterministic: the same ``(kind, id)`` always yields the same
    alias, so an alias printed in one message still resolves in the next. It is
    an ADDRESS, not a secret — resolution always goes back through the live
    pending list, so a stale or forged alias resolves to nothing rather than to
    the wrong item.
    """
    digest = hashlib.sha256(f"{kind}:{item_id}".encode("utf-8")).hexdigest()
    return f"{PENDING_ALIAS_PREFIX}{digest[:6]}"


def resolve_pending_alias(alias: str, items: List[dict]) -> Optional[dict]:
    """The item ``alias`` names, or ``None`` when it names none or more than one.

    Two matches is a hash collision across the LIVE queue. Refusing is the only
    honest answer: acting on either one would decide a proposal the owner did
    not look at.
    """
    if not alias:
        return None
    want = str(alias).strip().lower()
    hits = [it for it in items
            if pending_tap_alias(it.get("kind", ""), it.get("id", "")) == want]
    return hits[0] if len(hits) == 1 else None


def pending_kind_label(kind: str) -> str:
    """Short display label for a self-evolution pending-proposal ``kind``.

    Falls back to the raw ``kind`` string for anything outside the six known
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


# --- A27: the curated-notes pipeline -----------------------------------------
#
# Curated notes live in the memory backend's ``memory.db`` (the ``curated_memory``
# table), NOT in a per-tenant file quarantine like the other pending kinds. A
# forged/autonomous turn writes a note ``status='pending'`` (see
# ``tools/controller/action_registration.py``); an owner promotes it to
# ``active`` or rejects it (archives) from the same seam every other pending
# kind uses.
#
# ⚠️ Layering: ``core`` may not import ``modules.memory`` (the layering ratchet
# forbids the upward edge), so this reads/writes the ``curated_memory`` table
# DIRECTLY through the core-tier ``core.sqlite_util`` primitive — the same
# pattern ``core.status_snapshot`` uses to read agent/module DBs without reaching
# up. The store's own ``note_update(status=…)`` (A27) is the agent/provider-facing
# twin over the identical column.

def _curated_db_path(home_dir: "Path | str") -> Path:
    import os
    return Path(os.path.join(str(home_dir), "memory.db"))


def _curated_norm_user(user_id) -> str:
    from core.identity import normalize_user_id
    return normalize_user_id(user_id)


def _curated_list_pending(home_dir: "Path | str", user_id: str) -> List[dict]:
    """A tenant's PENDING curated notes as ``list_pending`` items (never raises).

    ⚠️ Never CREATES ``memory.db`` (status SSOT rule): returns [] when it is
    absent, so a pending-review read on a fresh install builds nothing."""
    db = _curated_db_path(home_dir)
    if not db.exists():
        return []
    try:
        from core.sqlite_util import execute_retry
        rows = execute_retry(
            str(db),
            "SELECT id, title, content FROM curated_memory "
            "WHERE user_id = ? AND COALESCE(status, 'active') = 'pending' "
            "ORDER BY COALESCE(updated_ts, 0) DESC, id DESC",
            (_curated_norm_user(user_id),), fetch="all") or []
    except Exception as e:
        logger.debug("self_evolution: curated list failed: %s", e)
        return []
    out: List[dict] = []
    for r in rows:
        content = (r["content"] or "").strip()
        title = (r["title"] or "").strip()
        preview = f"{title}: {content}" if title else content
        out.append({
            "kind": KIND_CURATED_NOTE,
            "id": str(r["id"]),
            "preview": preview,
            "chars": len(content),
            "path": None,   # DB-backed, not a file quarantine
        })
    return out


def _curated_set_status(home_dir: "Path | str", user_id: str, item_id,
                        status: str) -> bool:
    """Transition a PENDING curated note's status (promote -> active, reject ->
    archived). Tenant-scoped. False on a miss / absent db / bad id / error.

    ⚠️ Only a note whose current status is ``pending`` transitions — the owner
    review decides a PENDING proposal, never a live one. The ``AND … = 'pending'``
    predicate makes a ``reject`` (or ``promote``) with an arbitrary own-tenant id
    a no-op refusal rather than silently archiving an ACTIVE note (mirrors the
    skill pending-decide guard; ``list_pending`` only ever surfaces pending rows,
    so this closes the direct-POST path)."""
    db = _curated_db_path(home_dir)
    if not db.exists():
        return False
    try:
        note_id = int(item_id)
    except (TypeError, ValueError):
        return False
    try:
        import time
        from core.sqlite_util import execute_retry
        n = execute_retry(
            str(db),
            "UPDATE curated_memory SET status = ?, updated_ts = ? "
            "WHERE id = ? AND user_id = ? "
            "AND COALESCE(status, 'active') = 'pending'",
            (status, int(time.time()), note_id, _curated_norm_user(user_id)))
        return bool(n)
    except Exception as e:
        logger.debug("self_evolution: curated status set failed: %s", e)
        return False


def _curated_note_body(home_dir: "Path | str", user_id: str, item_id):
    """One curated note's full body for review (``show``), or None."""
    db = _curated_db_path(home_dir)
    if not db.exists():
        return None
    try:
        note_id = int(item_id)
    except (TypeError, ValueError):
        return None
    try:
        from core.sqlite_util import execute_retry
        row = execute_retry(
            str(db),
            "SELECT title, content FROM curated_memory WHERE id = ? AND user_id = ?",
            (note_id, _curated_norm_user(user_id)), fetch="one")
    except Exception as e:
        logger.debug("self_evolution: curated body read failed: %s", e)
        return None
    if row is None:
        return None
    title = (row["title"] or "").strip()
    content = row["content"] or ""
    return f"{title}\n\n{content}" if title else content

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

    # A27: curated notes quarantined by a forged/autonomous turn — the
    # sixth pending pipeline, DB-backed (memory.db) not a file quarantine.
    items.extend(_curated_list_pending(home_dir, user_id))

    # 035 P0-5: name the ACTIVE lines this draft appears to contradict, so a
    # stale promoted rule can never silently out-rank the owner's newer one.
    for it in items:
        conflicts = _conflicts_for(it, home_dir, instance_id, user_id)
        if conflicts:
            it["conflicts"] = conflicts

    return items


def _self_mod_ev(kind: str, action: str, item_id: str, user_id: str, ok: bool) -> None:
    """T4-06: record the owner's promote/reject decision as a first-class
    self_modification event on the durable log. Fail-open."""
    if not ok:
        return
    try:
        from core.self_events import emit_self_modification
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
    clear_notified_item(user_id, kind, item_id, home_dir=home_dir,
                        instance_id=instance_id)
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
    if kind == KIND_CURATED_NOTE:
        ok = _curated_set_status(home_dir, user_id, item_id, "active")
        _self_mod_ev(kind, "promote", item_id, user_id, ok)
        return ok, (f"note #{item_id} promoted (active)" if ok
                    else f"no pending note #{item_id}")
    return False, f"unknown pending kind: {kind!r}"


def reject(kind: str, item_id: str, *, user_id: str, home_dir: Path | str,
           instance_id: str, skill_manager=None) -> Tuple[bool, str]:
    """Reject (archive-then-discard) a pending proposal. Returns ``(ok, message)``."""
    # 019 #1: see promote() — owner action resets the notification batch.
    clear_notified_item(user_id, kind, item_id, home_dir=home_dir,
                        instance_id=instance_id)
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
    if kind == KIND_CURATED_NOTE:
        ok = _curated_set_status(home_dir, user_id, item_id, "archived")
        _self_mod_ev(kind, "reject", item_id, user_id, ok)
        return ok, (f"note #{item_id} rejected (archived)" if ok
                    else f"no pending note #{item_id}")
    return False, f"unknown pending kind: {kind!r}"


def decide_all(approve: bool, *, user_id: str, home_dir: Path | str,
               instance_id: str, skill_manager=None) -> Tuple[int, int, List[str]]:
    """Promote (or reject) EVERY pending proposal for a tenant. 035 P1-10.

    Returns ``(ok_count, fail_count, messages)``. Bulk review keeps a large
    queue manageable; requiring the owner to name each item creates friction
    precisely when the queue has grown.

    Iterates a SNAPSHOT: promoting a single-slot kind mutates the live set.
    An empty queue is a no-op, never an error.
    """
    items = list_pending(user_id, home_dir=home_dir, instance_id=instance_id,
                         skill_manager=skill_manager)
    fn = promote if approve else reject
    ok_n = fail_n = 0
    msgs: List[str] = []
    for it in items:
        try:
            ok, msg = fn(it["kind"], it["id"], user_id=user_id, home_dir=home_dir,
                         instance_id=instance_id, skill_manager=skill_manager)
        except Exception as e:  # one bad item must not abandon the rest
            ok, msg = False, f"{it['kind']}:{it['id']} failed: {e}"
        ok_n, fail_n = (ok_n + 1, fail_n) if ok else (ok_n, fail_n + 1)
        msgs.append(f"{'✓' if ok else '✗'} {it['kind']}:{it['id']} — {msg}")
    return ok_n, fail_n, msgs


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
        if kind == KIND_CURATED_NOTE:
            # A27: a curated note is a DB row, not a file quarantine — read its
            # body from the store rather than a path.
            body = _curated_note_body(home_dir, user_id, item_id)
            if body is None:
                return False, f"could not read pending note #{item_id}"
        else:
            body = Path(match["path"]).read_text(encoding="utf-8")
        if len(body) > cap_chars:
            body = body[:cap_chars] + f"\n[... truncated at {cap_chars} chars]"
        return True, body
    except Exception as e:
        logger.debug("self_evolution: show failed: %s", e)
        return False, f"could not read pending {kind} '{item_id}': {e}"


def pending_tap_token(verb: str, item: dict) -> str:
    """The ONE auto-linked token that decides ``item`` — e.g. ``/approve_p_a1b2c3``.

    ``verb`` is ``approve`` or ``reject``. Every owner surface renders a decision
    through this, so the token the owner taps and the token the router resolves
    cannot drift apart.
    """
    alias = pending_tap_alias(item.get("kind", ""), item.get("id", ""))
    return f"/{verb}_{alias.replace('-', '_')}"


def build_pending_notification(items: List[dict]) -> Optional[str]:
    """Turn a pending set into one proactive owner message (or None if empty).

    Kept terse: the owner scans it on a phone. Names each proposal + how to act.

    2026-09-15: the closing line used to read ``Reply "approve" / "reject"``
    plus a ``polyrob owner pending`` CLI command. Neither worked from the seat
    the owner reads this on. Nothing parsed a bare ``approve`` — the word fell
    through to the agent as chat, and the agent has no promote verb — and a
    phone owner has no shell. So the one instruction on screen did nothing, on
    both halves. Every item now carries its own tappable token, and the footer
    names chat verbs only.
    """
    if not items:
        return None
    lines = [f"🧠 I've proposed {len(items)} change(s) to how I work — tap to approve:"]
    for it in items:
        if it["kind"] == KIND_SELF_CONTEXT:
            label = "identity note"
        elif it["kind"] == KIND_OWNER:
            label = "owner-facts note"
        elif it["kind"] == KIND_CONTRACT:
            label = "operating contract"
        elif it["kind"] == KIND_PREF_CHANGE:
            label = f"preference change '{it['id']}'"
        elif it["kind"] == KIND_CURATED_NOTE:
            label = f"note #{it['id']}"
        else:
            label = f"skill '{it['id']}'"
        preview = (it.get("preview") or "").strip()
        if len(preview) > 140:
            preview = preview[:137] + "…"
        lines.append(f"• {label} — {preview}")
        # 035 P0-5: a contradiction is the reason this needs the owner NOW.
        for c in (it.get("conflicts") or [])[:2]:
            lines.append(f"   ⚠ CONFLICT — {c}")
        lines.append(f"   {pending_tap_token('approve', it)}   "
                     f"{pending_tap_token('reject', it)}")
    lines.append("Everything at once: /approve_all · the full list: /pending")
    return "\n".join(lines)


# --- 035 P0-1: pending-notification state (per item, content-aware) ----------
#
# HISTORY. ``maybe_notify_owner_pending`` used to fire on EVERY pending write —
# on 2026-07-18, 29 of the 30 shared daily delivery slots were burned by it,
# starving the daily digest (proposal 019). 019's fix fingerprinted the pending
# SET (a hash of the sorted ``kind:id`` pairs) and skipped the push when the set
# was UNCHANGED. That hash was **content-independent by design**, and the
# identity kinds are **single-slot** (one ``self.md``/``owner.md`` per tenant, so
# ``id`` is always the tenant id). Consequence, measured on prod:
#
#   2026-09-08 14:01:34  a background_review skill proposal filled the 4th slot.
#                        The notice "I've proposed 4 change(s) to how I work"
#                        was SUPPRESSED (outcome=capped, bucket=lifecycle) — yet
#                        ``capped`` was in ``_NOTIFIED_OUTCOMES``, so the set was
#                        recorded as notified.
#   from that second on   every revision of those 4 slots hashed identically, so
#                        the owner's four "stop posting to the den" directives
#                        (09-08 15:28/16:16/16:33, 09-09 16:19) returned before
#                        attempting delivery. The lock clears only on a
#                        promote/reject, which needs the owner to know, which
#                        needs the notice. The deadlock fed itself.
#
# 035 keeps 019's intent and removes the lock: state is per ``kind:id`` -> a hash
# of the item's CONTENT. An unchanged draft still never re-notifies; a REVISED
# draft always does. Persisted next to the pending stores (the tenant's identity
# tier root, ``core.instance.self_tier_root``); the module-level dict is only the
# fallback for tenant ids the identity store refuses.
#
# The legacy opaque fingerprint file is RETIRED, never read: an upgrade therefore
# re-notifies exactly once, which is how the four proposals stuck on prod recover.

#: Retired by 035. Deleted on the first state write, never consulted.
_LEGACY_FINGERPRINT_FILE = ".last_notified_pending"
_NOTIFY_STATE_FILE = ".notified_pending.json"
_notify_states: dict = {}  # (home_dir, instance_id, user_id) -> {key: content_hash}

# Rail outcomes after which an item counts as "notified" — i.e. the owner
# PLAUSIBLY SAW it. ``sent`` is a live delivery; ``deduped`` means the identical
# text reached them inside the dedup window; ``fallback`` is the durable
# owner_notice written when no live sink exists (a local/REPL owner's only
# channel, so it is their normal path, not a suppression).
#
# 035 P0-1 removed ``capped`` and ``quiet_held``. Both are SUPPRESSIONS: the
# message did not reach the owner, and the durable ``owner_notice`` row they
# leave behind is visible only via ``polyrob telemetry``. Counting an unread
# record as delivery is what made the 09-08 lock permanent. ``rate_limited``
# was already excluded on the same reasoning.
_NOTIFIED_OUTCOMES = ("sent", "deduped", "fallback")


def _item_key(item: dict) -> str:
    return f"{item.get('kind', '')}:{item.get('id', '')}"


def _item_content_hash(item: dict) -> str:
    """Hash of the pending item's CONTENT.

    Reads the quarantined file when its path is readable — the only way to see a
    revision that preserves both length and the first ~160 chars of preview.
    Falls back to ``chars:preview`` so an unreadable path still yields a stable,
    revision-sensitive value rather than a crash.
    """
    raw = ""
    path = item.get("path")
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
    if not raw:
        raw = f"{item.get('chars', '')}:{item.get('preview', '')}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def pending_notify_state(items: List[dict]) -> dict:
    """``{kind:id -> content_hash}`` for a pending set. Pure."""
    return {_item_key(it): _item_content_hash(it) for it in items}


def pending_needs_notification(items: List[dict], state: Optional[dict]) -> bool:
    """True when any item is NEW or REVISED since *state* was recorded. Pure.

    This is the whole anti-spam policy: an untouched draft is silent forever, a
    changed one always speaks. Nothing here depends on how many kinds exist, so a
    single-slot kind can no longer lock the notifier (035 P0-1).
    """
    if not items:
        return False
    known = state or {}
    for it in items:
        key = _item_key(it)
        if key not in known or known[key] != _item_content_hash(it):
            return True
    return False


def _tier_root(user_id: str, home_dir: Path | str, instance_id: str) -> Path:
    from core.instance import self_tier_root
    return self_tier_root(home_dir, user_id, instance_id)


def _state_path(user_id: str, home_dir: Path | str,
                instance_id: str) -> Optional[Path]:
    try:
        from core.instance import is_safe_tenant_id
        if not is_safe_tenant_id(user_id):
            return None
        return _tier_root(user_id, home_dir, instance_id) / _NOTIFY_STATE_FILE
    except Exception:
        return None


def load_notified_state(user_id: str, *, home_dir: Path | str,
                        instance_id: str) -> dict:
    """The last-notified content hashes for this tenant. Fail-open to ``{}``.

    ``{}`` means "notify" — failing open toward TELLING the owner is the correct
    direction for this rail; the 09-08 incident was a failure to speak.
    """
    path = _state_path(user_id, home_dir, instance_id)
    if path is not None:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            logger.debug("self_evolution: notify state read failed: %s", e)
    return dict(_notify_states.get(
        (str(home_dir), str(instance_id), str(user_id)), {}))


def save_notified_state(user_id: str, state: dict, *, home_dir: Path | str,
                        instance_id: str) -> None:
    _notify_states[(str(home_dir), str(instance_id), str(user_id))] = dict(state)
    path = _state_path(user_id, home_dir, instance_id)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        # Retire the 019 fingerprint file so it can never be resurrected.
        (path.parent / _LEGACY_FINGERPRINT_FILE).unlink(missing_ok=True)
    except Exception as e:
        logger.debug("self_evolution: notify state write failed: %s", e)


def clear_notified_item(user_id: str, kind: str, item_id: str, *,
                        home_dir: Path | str, instance_id: str) -> None:
    """Forget ONE item (the owner promoted/rejected it), so a later proposal
    under the same id notifies again. Fail-open."""
    state = load_notified_state(user_id, home_dir=home_dir, instance_id=instance_id)
    state.pop(f"{kind}:{item_id}", None)
    save_notified_state(user_id, state, home_dir=home_dir, instance_id=instance_id)


# --- 035 P1-11: report-after ------------------------------------------------
#
# An immediate rule change happens on an owner turn, so the owner is right there
# — the report belongs in the agent's reply, not in another proactive push the
# daily cap would have to carry. One line, no diff library.


def summarize_doc_change(before: Optional[str], after: Optional[str]) -> str:
    """One owner-readable line describing a document change. Pure, never raises."""
    try:
        old_lines = [ln.strip() for ln in (before or "").splitlines() if ln.strip()]
        new_lines = [ln.strip() for ln in (after or "").splitlines() if ln.strip()]
        old_set, new_set = set(old_lines), set(new_lines)
        added = [ln for ln in new_lines if ln not in old_set]
        removed = [ln for ln in old_lines if ln not in new_set]
        if not added and not removed:
            return "no change"
        parts = [f"+{len(added)}/-{len(removed)} lines"]
        if added:
            head = added[0]
            parts.append(f'added: "{head[:100]}"' + ("…" if len(head) > 100 else ""))
        elif removed:
            head = removed[0]
            parts.append(f'removed: "{head[:100]}"' + ("…" if len(head) > 100 else ""))
        return "; ".join(parts)
    except Exception:
        return "changed"


# --- 035 P0-5: a pending rule that contradicts an ACTIVE one -----------------
#
# On 09-08 the ACTIVE self.md said "Telegram den = public-facing content only"
# while the PENDING draft said "STOP ALL posting to the Telegram public den".
# Promotion alone would not have been enough — the new rule had to SUPERSEDE an
# older promoted one, and nothing detected that. Deterministic and LLM-free: a
# prohibition in the draft whose subject also appears in a NON-prohibiting active
# line is a contradiction worth naming.

_PROHIBITION_RE = re.compile(
    r"\b(no|not|never|stop|cease|halt|don'?t|do not|avoid|forbid(?:den)?|"
    r"disallow(?:ed)?|prohibit(?:ed)?)\b", re.IGNORECASE)

_CONFLICT_STOPWORDS = frozenset({
    "the", "and", "for", "all", "any", "with", "from", "this", "that", "into",
    "only", "your", "will", "must", "should", "until", "when", "what", "content",
    "rule", "owner", "directive", "priority", "highest", "effective",
    "immediately", "overrides", "policy", "posting", "posts", "post", "report",
    "reporting", "never", "stop", "always",
})

#: How many significant terms a pending prohibition and an active line must share
#: before the pair is called a contradiction. Two is enough to bind a subject
#: ("telegram" + "den") and high enough to ignore incidental word reuse.
_CONFLICT_MIN_OVERLAP = 2


def _significant_terms(line: str) -> set:
    """Content words of a line, >=3 chars, minus stopwords.

    Hyphens SPLIT (they are not word characters here) — otherwise
    "public-facing" and "public" are different terms and the 09-08 den case
    scores 1 overlap instead of 2, which is exactly the pair this must catch.
    "NO-DEN" likewise yields "den".
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", line.lower())
    return {w for w in words if w not in _CONFLICT_STOPWORDS}


def detect_conflicts(pending_text: str, active_text: str) -> List[str]:
    """Active lines a pending draft appears to contradict. Pure, LLM-free.

    Returns owner-readable strings, most specific first, or ``[]``. Deliberately
    conservative: it only fires when the draft PROHIBITS something and an active
    line that does NOT prohibit it shares at least
    :data:`_CONFLICT_MIN_OVERLAP` significant terms. Two prohibitions about the
    same subject agree, and are not reported.
    """
    if not pending_text or not active_text:
        return []
    try:
        bans = [ln.strip() for ln in pending_text.splitlines()
                if ln.strip() and _PROHIBITION_RE.search(ln)]
        if not bans:
            return []
        ban_terms = set()
        for ln in bans:
            ban_terms |= _significant_terms(ln)
        if not ban_terms:
            return []
        out: List[str] = []
        seen = set()
        for ln in active_text.splitlines():
            line = ln.strip().lstrip("#-* ").strip()
            if not line or _PROHIBITION_RE.search(line):
                continue
            overlap = ban_terms & _significant_terms(line)
            if len(overlap) < _CONFLICT_MIN_OVERLAP:
                continue
            shared = ", ".join(sorted(overlap)[:4])
            msg = (f"the ACTIVE doc still says: \"{line[:140]}\" "
                   f"(shares: {shared})")
            if msg not in seen:
                seen.add(msg)
                out.append(msg)
        return out[:5]
    except Exception as e:  # never let advisory detection break a listing
        logger.debug("self_evolution: conflict detection failed: %s", e)
        return []


#: Which ACTIVE doc a pending kind would replace. Skills and pref changes have no
#: single active prose doc, so they are not conflict-checked.
_ACTIVE_LOADER_BY_KIND = {
    KIND_SELF_CONTEXT: "load_self_doc",
    KIND_OWNER: "load_owner_doc",
    KIND_CONTRACT: "load_contract_doc",
}


def _conflicts_for(item: dict, home_dir: Path | str, instance_id: str,
                   user_id: str) -> List[str]:
    loader_name = _ACTIVE_LOADER_BY_KIND.get(item.get("kind", ""))
    if not loader_name:
        return []
    try:
        import core.instance as _inst
        active = getattr(_inst, loader_name)(home_dir, user_id, instance_id)
        if not active:
            return []
        path = item.get("path")
        pending = Path(path).read_text(encoding="utf-8", errors="replace") if path else ""
        return detect_conflicts(pending, active)
    except Exception as e:
        logger.debug("self_evolution: conflict lookup failed: %s", e)
        return []



def _record_owner_notice(text: str) -> None:
    """T4-04 fallback: persist an owner-facing notice to the durable event log when a
    live push couldn't be delivered, so a REPL/local owner (no telegram daemon) still
    sees it via `polyrob telemetry` and the message is never silently lost. Owner-scoped,
    fail-open.

    ⚠️ A7 / A40 (2026-09-14): this writes an UNMARKED ``owner_notice`` row — it
    does not carry one of ``user_delivery.NOTICE_MARKERS``, so
    ``core.surfaces.missed.missed_notices`` (and every ``/missed`` seat) cannot
    see it. Has zero callers today; before wiring one, prefix ``text`` with a
    ``NOTICE_MARKERS`` entry (or add one) so the notice is actually readable.
    """
    try:
        from core.event_log import get_event_log
        from core.instance import resolve_owner_user_id
        owner = resolve_owner_user_id()
        get_event_log().record("owner_notice", user_id=str(owner),
                                source="self_evolution", text=str(text)[:2000])
    except Exception:
        # Fail-open by design, but never silent: a lost owner notice is the
        # "blind owner" class (2026-08-28 prod forensics).
        logger.warning("self_evolution: owner_notice event not recorded", exc_info=True)


async def _push_owner_message_outcome(container, text: Optional[str],
                                      attachments: Optional[list] = None,
                                      priority: Optional[str] = None,
                                      source: Optional[str] = None) -> Optional[str]:
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
        from core.instance import resolve_owner_user_id
        from core.surfaces.user_delivery import deliver_user_message
        owner = ""
        try:
            owner = str(resolve_owner_user_id())
        except Exception:
            owner = ""
        return await deliver_user_message(
            container, owner, str(text), source=source or "self_evolution",
            attachments=attachments, priority=priority)
    except Exception as e:  # never let a notification failure break a write
        logger.debug("self_evolution: owner notify failed (fail-open): %s", e)
        return None


async def push_owner_message(container, text: Optional[str],
                             attachments: Optional[list] = None,
                             priority: Optional[str] = None,
                             source: Optional[str] = None) -> bool:
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

    ``source`` (2026-08-18): names the DELIVERY LANE. This used to be hardcoded
    ``"self_evolution"``, so a blocked-goal escalation ("I stopped, I need you")
    was indistinguishable at the cap gate from a "goal started" ping. Pass
    ``"goal_blocked"`` for a need the agent cannot proceed without — that source
    is in ``_CRITICAL_SOURCES`` and the daily cap may not drop it. Omitted =>
    ``self_evolution``, unchanged.
    """
    return (await _push_owner_message_outcome(
        container, text, attachments=attachments, priority=priority,
        source=source)) == "sent"


async def maybe_notify_owner_pending(container, user_id: str, *, home_dir: Path | str,
                                     instance_id: str, skill_manager=None) -> bool:
    """Notify the owner of this tenant's pending proposals if transparency is enabled.

    Gated ``SELF_EVOLUTION_TRANSPARENCY`` (default OFF on server, ON under
    POLYROB_LOCAL). No-op + fail-open otherwise.

    019 #1 / 035 P0-1 — batched: skips the push when NO pending item is new or
    REVISED since the last notification (per-``kind:id`` content hashes persisted
    under the tenant's identity tier root), so a goal that repeatedly touches the
    same pending draft can no longer burn the shared daily delivery cap. A new
    item — or a revision of an existing single-slot draft, which 019's set-keyed
    fingerprint could not see — still notifies promptly.
    """
    try:
        if not AutonomyConfig.self_evolution_transparency():
            return False
    except Exception:
        return False
    items = list_pending(user_id, home_dir=home_dir, instance_id=instance_id,
                         skill_manager=skill_manager)
    if not items:
        # Set went empty (everything promoted/rejected/expired): forget the
        # whole batch so the next pending item always notifies.
        save_notified_state(user_id, {}, home_dir=home_dir,
                            instance_id=instance_id)
        return False
    state = load_notified_state(user_id, home_dir=home_dir, instance_id=instance_id)
    if not pending_needs_notification(items, state):
        logger.debug("self_evolution: no new or revised pending item since the "
                     "last notification; skipping owner notify")
        return False
    msg = build_pending_notification(items)
    # 035 P0-2: NOT the default ``self_evolution`` source. That lane is the
    # lifecycle bucket ("goal started"/"goal completed" chatter) and on 09-08 it
    # was exhausted 40 minutes before this exact message was attempted, which
    # returned ``capped``. "Approve my new rules" is an owner DECISION the agent
    # is blocked on — the same class as ``approval``/``goal_blocked`` — so it
    # rides a critical source the daily cap may not drop.
    outcome = await _push_owner_message_outcome(container, msg,
                                               source=NOTIFY_SOURCE)
    if outcome in _NOTIFIED_OUTCOMES:
        save_notified_state(user_id, pending_notify_state(items),
                            home_dir=home_dir, instance_id=instance_id)
    return outcome == "sent"


__all__ = [
    "KIND_SELF_CONTEXT", "KIND_SKILL", "KIND_OWNER", "KIND_CONTRACT", "KIND_PREF_CHANGE",
    "PENDING_KIND_LABELS", "pending_kind_label",
    "list_pending", "promote", "reject", "decide_all", "show", "build_pending_notification",
    "push_owner_message", "maybe_notify_owner_pending",
    "NOTIFY_SOURCE", "detect_conflicts", "summarize_doc_change",
    "pending_notify_state", "pending_needs_notification",
    "load_notified_state", "save_notified_state", "clear_notified_item",
]
