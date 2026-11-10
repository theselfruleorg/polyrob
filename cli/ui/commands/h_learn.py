"""`/learn <description>` — distill a described procedure into a live skill.

The owner describes a procedure in prose; the agent distills it into a SKILL.md
body and activates it via ``SkillManager.create_skill`` (created_by=user: the owner
taught it, so the curator never auto-archives it). No new safety machinery — this is a thin front-door onto SkillWriterMixin.

The distillation is deterministic by default (a structured template wrapping the
description); a model turn is not required, so /learn never blocks on or costs an
LLM call. An id that already has a skill is refused: the template must not replace it.
"""
import re
from typing import Optional, Tuple

_STOPWORDS = {"the", "a", "an", "to", "of", "and", "or", "for", "when", "how",
              "always", "never", "should", "with", "your", "you", "this", "that"}


def _slug(description: str) -> str:
    """Derive a valid skill_id (``^[a-z][a-z0-9-]*$``) from the description."""
    words = re.findall(r"[a-z0-9]+", (description or "").lower())
    meaningful = [w for w in words if w not in _STOPWORDS] or words
    slug = "-".join(meaningful[:5])[:48].strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug or not slug[0].isalpha():
        slug = "learned-" + slug if slug else "learned-skill"
    return slug


def _distill(description: str) -> Tuple[str, str]:
    """Turn a prose description into (skill_id, SKILL.md body). Deterministic."""
    desc = (description or "").strip()
    skill_id = _slug(desc)
    title = desc.split(".")[0].strip()[:60] or "Learned procedure"
    content = (
        f"# {title}\n\n"
        f"## When to use\n\n{desc}\n\n"
        f"## Procedure\n\n{desc}\n"
    )
    return skill_id, content


def _skill_manager(ctx):
    """Resolve the SkillManager singleton (seam for tests)."""
    from agents.task.agent.skill_manager import get_skill_manager
    return get_skill_manager()


def h_learn(ctx) -> None:
    """REPL handler: /learn <freeform description of a procedure>."""
    description = " ".join(getattr(ctx, "args", None) or []).strip()
    if not description:
        ctx.emit("usage: /learn <describe a procedure to save as a skill>",
                  title="learn")
        return
    uid = (getattr(ctx, "user_id", "") or "").strip() or "local"

    try:
        mgr = _skill_manager(ctx)
    except Exception as e:
        ctx.emit(f"(/learn unavailable: {e})", title="learn")
        return
    if mgr is None:
        ctx.emit("(/learn unavailable: no skill manager)", title="learn")
        return

    skill_id, content = _distill(description)
    # Format-validate the derived id (create_skill re-validates).
    try:
        ok, _errs = mgr.validate_skill_id(skill_id)
        if not ok:
            ctx.emit(f"/learn could not derive a valid skill id from: {description[:60]}",
                      title="learn")
            return
    except Exception:
        pass

    active = getattr(mgr, "active_skill_text", None)
    if callable(active) and active(skill_id, user_id=uid):
        ctx.emit(f"/learn: the skill '{skill_id}' exists. Ask me to change it, or describe "
                 f"the new procedure in other words.", title="learn")
        return
    try:
        # The owner taught it: it is live at once, with the owner as the author.
        from modules.skills.writer import PROVENANCE_USER
        res = mgr.create_skill(
            skill_id, content, user_id=uid,
            description=(description[:200]), created_by=PROVENANCE_USER)
    except Exception as e:
        ctx.emit(f"(/learn failed to write skill: {e})", title="learn")
        return

    if not getattr(res, "ok", False):
        errs = "; ".join(getattr(res, "errors", []) or []) or "rejected"
        ctx.emit(f"/learn rejected: {errs}", title="learn")
        return

    sid = getattr(res, "skill_id", skill_id)
    if getattr(res, "pending", False):
        ctx.emit(
            f"Learned '{sid}' — saved as a PENDING skill for your review.\n"
            f"Promote it with:  /pending promote skill {sid}",
            title="learn")
    else:
        ctx.emit(f"Learned '{sid}' — active.", title="learn")
