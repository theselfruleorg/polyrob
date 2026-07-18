"""`/learn <description>` — distill a described procedure into a quarantined skill.

The owner describes a procedure in prose; the agent distills it into a SKILL.md
body and lands it in ``.pending/`` via ``SkillManager.create_skill`` (created_by=
agent), so the existing quarantine + identity-scan + owner-promote flow applies
unchanged. No new safety machinery — this is a thin front-door onto SkillWriterMixin.

The distillation is deterministic by default (a structured template wrapping the
description); a model turn is not required, so /learn never blocks on or costs an
LLM call. The owner promotes the result with `/pending promote skill <id>`.
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
        ctx.emit("usage: /learn <describe a procedure to distill into a pending skill>",
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
    # Format-validate the derived id (create_skill re-validates + owns any
    # same-id overwrite, archiving the prior body). We don't pre-check existence:
    # re-learning the same description intentionally refines the same pending draft.
    try:
        ok, _errs = mgr.validate_skill_id(skill_id)
        if not ok:
            ctx.emit(f"/learn could not derive a valid skill id from: {description[:60]}",
                      title="learn")
            return
    except Exception:
        pass

    try:
        # pending=True FORCES quarantine regardless of SKILLS_WRITABLE_REQUIRE_REVIEW:
        # a described procedure is always owner-reviewed before it can auto-trigger.
        res = mgr.create_skill(
            skill_id, content, user_id=uid,
            description=(description[:200]), created_by="agent", pending=True)
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
