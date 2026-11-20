"""095 W4 — the parts of the ``<state-delta>`` channel that are not health.

``MessageManager.update_state_delta`` is the ONE writer (a durable message at the
tail, pushed only when a part's text changed). This module computes the parts
whose source is not the status snapshot:

* ``self_facts`` — the model, provider and image route after a model change
  (``ModelSwapMixin._refresh_after_model_change``);
* ``style_prefs`` / ``owner_rules`` / ``self_notes`` — re-read at the step
  boundary when the preferences file, ``owner.md`` or the SELF doc changed
  (``core.prefs_hooks`` epoch + the files' mtimes), so a SAFE style key applies
  from the agent's next step, not its next session;
* ``skills_delta`` — the skill index lines a ``load_tool`` added or removed.

Every function is fail-open: a broken part is skipped, never a broken step.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


def self_facts_text(agent, cause: str, previous_model: Optional[str]) -> str:
    """The ``self_facts`` part after a model change (full current text)."""
    model = getattr(agent, "model_name", None) or "unknown"
    provider = (getattr(agent, "llm_provider", None)
                or getattr(agent, "provider_name", None) or "unknown")
    # A room or a correspondent session: the model is not named (see set_runtime_identity).
    mm = getattr(agent, "message_manager", None)
    try:
        from agents.task.agent.messages.foundation_layers import identity_hidden
        hidden = mm is not None and identity_hidden(mm)
    except Exception:
        hidden = True
    line = ("Your model changed (not named in this chat)" if hidden
            else f'Model now: "{model}" via provider "{provider}"')
    if previous_model and previous_model != model and not hidden:
        who = {"owner": "the owner switched it", "api": "an API request switched it",
               "fallback": "a provider failover, not the owner",
               "restore": "restored after a failover"}.get(cause, cause)
        line += (f' (changed from "{previous_model}" at '
                 f'{time.strftime("%H:%MZ", time.gmtime())}; {who})')
    images = "you can see images" if getattr(agent, "use_vision", False) else \
        "you cannot see images (an attached image reaches you as text only)"
    return f"{line}.\nImages: {images}."


# --- owner self-context (prefs, owner.md, SELF doc) -------------------------------

def _owner_state_allowed(agent) -> bool:
    from agents.task.agent.core.live_health import _operations_injected
    return _operations_injected(agent)


def _sources(agent):
    from core.instance import resolve_instance_id, self_tier_root
    from core.prefs import preferences_path
    from core.runtime_paths import prefs_home_dir
    home = prefs_home_dir()
    uid = getattr(getattr(agent, "orchestrator", None), "user_id", None) \
        or getattr(agent, "user_id", None)
    iid = resolve_instance_id()
    root = self_tier_root(home, uid, iid)
    return home, uid, iid, (preferences_path(home, uid, iid),
                            root / "owner.md", root / "self.md",
                            # 127 W3: a draft written or rejected moves only this file.
                            root / ".pending" / "owner.md")


def _mtime(path) -> float:
    try:
        return path.stat().st_mtime_ns if path is not None else 0
    except OSError:
        return 0


#: 127 W3: over this, the owner-rules part carries the changed lines, not the doc
#: (the state-delta part cap is 4000 chars; owner.md may hold 24000).
_OWNER_PART_FULL_MAX = 3200


def _owner_rules_part(agent, owner: str, uid, home, iid) -> str:
    """The rules as they are now, headed by whether a draft waits (127 W3): the owner
    decides outside the agent's turn, and an old tool result in the history still
    says "tap to approve". A long doc sends what changed plus a pointer."""
    try:
        from core.status_rules import owner_draft_status
        status = owner_draft_status(uid, home_dir=home, instance_id=iid)
    except Exception:
        status = "Owner-rule drafts: unreadable — say you cannot tell."
    seen = getattr(agent, "_owner_rules_seen", None)
    last = getattr(agent, "_owner_rules_part_last", None)
    if seen == owner and isinstance(last, tuple) and last[:2] == (owner, status):
        # Nothing in the rules or the draft moved (a preferences write woke the check):
        # the same text, so the part is not pushed again as "(no rule line changed)".
        return last[2]
    text = _render_owner_rules_part(owner, status, seen)
    try:
        agent._owner_rules_seen = owner
        agent._owner_rules_part_last = (owner, status, text)
    except Exception:
        logger.debug("owner rules part not cached", exc_info=True)
    return text


def _render_owner_rules_part(owner: str, status: str, seen) -> str:
    if len(owner) <= _OWNER_PART_FULL_MAX or seen is None:
        body = owner if len(owner) <= _OWNER_PART_FULL_MAX else (
            "(the full rules are in the ## Owner rules block of your context; "
            "owner_doc_manage action='read' shows them)")
        return f"## Owner rules (current)\n{status}\n\n{body}"
    from core.doc_claims import changed_lines
    added, removed = changed_lines(seen, owner), changed_lines(owner, seen)
    lines = ["## Owner rules (changed)", status, ""]
    lines += [f"+ {l}" for l in added[:20]] + [f"- {l}" for l in removed[:20]]
    if not (added or removed):
        lines.append("(no rule line changed)")
    lines.append("All rules: owner_doc_manage action='read'.")
    return "\n".join(lines)


def self_context_parts(agent) -> dict:
    """The full current text of the three owner self-context parts."""
    from core.instance import load_owner_doc, load_self_doc
    from core.prefs import load_preferences, render_style_line
    home, uid, iid, _paths = _sources(agent)
    style = render_style_line(load_preferences(home, uid, iid)) or "Style: none set (defaults)."
    owner = load_owner_doc(home, uid, iid) or "(empty)"
    notes = load_self_doc(home, uid, iid) or "(empty)"
    return {
        "style_prefs": "The owner's reply style now (it overrides any earlier style "
                       f"line or reply-length budget):\n{style}",
        "owner_rules": _owner_rules_part(agent, owner, uid, home, iid),
        "self_notes": f"Your self notes (current):\n{notes}",
    }


def refresh_self_context(agent) -> None:
    """Step boundary (``UserIngressMixin._drain_user_messages``): when a
    preferences / ``owner.md`` / SELF write happened since the last check, push
    the parts whose text changed. The first call records the baseline the
    foundation already carries. Owner-memory regimes only (SK-18)."""
    try:
        manager = getattr(agent, "message_manager", None)
        if manager is None or not hasattr(manager, "update_state_delta"):
            return
        if not _owner_state_allowed(agent):
            if getattr(agent, "_self_context_fp", None) is not None:
                agent._self_context_fp = None
                manager.update_state_delta({k: "" for k in
                                            ("style_prefs", "owner_rules", "self_notes")})
            return
        from core.prefs_hooks import epoch
        _home, _uid, _iid, paths = _sources(agent)
        fp = (epoch(),) + tuple(_mtime(p) for p in paths)
        previous = getattr(agent, "_self_context_fp", None)
        if previous == fp:
            return
        agent._self_context_fp = fp
        manager.update_state_delta(self_context_parts(agent), baseline=previous is None)
    except Exception as e:
        logger.debug("self-context refresh skipped: %s", e)


# --- the skill index after load_tool ---------------------------------------------

def skills_delta_part(agent) -> Optional[str]:
    """After a ``load_tool`` (``controller._skill_index_stale``): the index
    lines now listed that were not, and the ids no longer listed. None = no
    load since the last check."""
    controller = getattr(agent, "controller", None)
    if controller is None or not getattr(controller, "_skill_index_stale", False):
        return None
    controller._skill_index_stale = False
    try:
        from agents.task.agent.skill_manager import catalog_line, get_skill_manager
        from agents.task.session_class import seat_is_third_party
        sm = get_skill_manager()
        user_id = getattr(getattr(agent, "orchestrator", None), "user_id", None)
        tool_ids = controller.list_tools()
        absent = controller.absent_tool_ids(sm.declared_tool_ids(user_id))
        controller._absent_tool_ids = absent
        session = getattr(controller, "_session_skills", None)
        if not isinstance(session, dict):
            return None
        baseline = getattr(controller, "_skill_index_baseline", None)
        if baseline is None:
            baseline = controller._skill_index_baseline = set(session)
        now = {s.skill_id: s for s in sm.get_catalog_skills(
            user_id=user_id, max_skills=None, tool_ids=tool_ids, absent=absent,
            third_party=seat_is_third_party(controller))}
        pinned = set(getattr(controller, "_auto_skills", None) or ())
        added = [now[i] for i in now if i not in baseline]
        removed = sorted(i for i in baseline if i not in now and i not in pinned)
        for skill in added:
            session.setdefault(skill.skill_id, skill)
        # A session hit is served by load_skill with no gate: an id that left the
        # index leaves the session set too (load_skill's fallback re-gates it).
        for sid in removed:
            session.pop(sid, None)
        lines = []
        if added:
            lines.append("Now in your skill index (load_skill):")
            lines.extend(catalog_line(s) for s in added)
        if removed:
            lines.append("No longer in your skill index: " + ", ".join(removed))
        return "\n".join(lines)
    except Exception as e:
        logger.debug("skill index delta skipped: %s", e)
        return None
