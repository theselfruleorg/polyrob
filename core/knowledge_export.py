"""Obsidian-vault export of the agent's knowledge (C3, 2026-07-11).

Composes the EXISTING readers — C1 notes, the episode ledger, the skill
catalog, identity docs, the goal board — into a per-tenant markdown vault:

    notes/<id>-<slug>.md      YAML frontmatter + body ([[wikilinks]] intact)
    notes/archived/…          archived notes (status in frontmatter)
    episodes/YYYY-MM-DD.md    daily notes (task/outcome/summary/artifacts/spend)
    skills/<skill_id>.md      SKILL.md body + provenance header
    identity/soul.md|self.md  operator SOUL / agent SELF docs
    goals.md                  goal board snapshot
    index.md                  the "what my agent knows" front page

Export-only projection: the DBs stay the SSOT, the vault is never a second
write path (no two-way sync). Every section is fail-open — a missing reader
yields an empty section, never a crash. All non-stdlib imports are lazy so
this module never drags the server tier in (core/server boundary, C3 split).
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")
_EPISODE_PAGE = 20          # provider clamps recall_episodes limit to 20
_EPISODE_MAX = 2000         # pagination backstop


def sanitize_filename(name: Optional[str]) -> str:
    """A safe, readable kebab-case file stem. Empty/None -> 'untitled'."""
    slug = _SLUG_RE.sub("-", str(name or "").strip()).strip("-")
    return slug or "untitled"


def _fmt_day(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts))) if ts else ""
    except Exception:
        return ""


def _yaml_value(v) -> str:
    """One safe YAML scalar/inline-list (json.dumps is valid YAML for these)."""
    return json.dumps(v, ensure_ascii=False)


def _frontmatter(fields: Dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in fields.items():
        if v in (None, "", []):
            continue
        lines.append(f"{k}: {_yaml_value(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- sections ----------------------------------------------------------------


async def _export_notes(out: Path, provider, user_id: str) -> int:
    if provider is None or not hasattr(provider, "note_list"):
        return 0
    count = 0
    # Pending notes are quarantined (unreviewed, possibly forged-turn-authored) —
    # they get their own subdir like archived ones, never mixed in with vetted
    # active notes (mirrors the pending-skills separation in /knowledge).
    for status, subdir in (("active", ""), ("pending", "pending"),
                           ("archived", "archived")):
        try:
            notes = await provider.note_list(user_id, status=status, limit=1000)
        except Exception:
            notes = []
        for n in notes:
            stem = f"{n['id']}-{sanitize_filename(n.get('title'))}"
            front = _frontmatter({
                "id": n.get("id"), "title": n.get("title") or "",
                # Obsidian resolves [[wikilinks]] by FILENAME or aliases — never by
                # a title: field. POLYROB's links are title-based, so the title must
                # ride as an alias for [[My Note]] to hit `<id>-my-note.md`.
                "aliases": [n["title"]] if n.get("title") else [],
                "tags": n.get("tags") or [], "status": n.get("status"),
                "source": n.get("source") or "",
                "created": _fmt_day(n.get("created_ts")),
                "updated": _fmt_day(n.get("updated_ts")),
                "created_by": n.get("created_by") or "",
            })
            body = (n.get("content") or "").rstrip() + "\n"
            target = out / "notes" / subdir / f"{stem}.md" if subdir else out / "notes" / f"{stem}.md"
            _write(target, front + body)
            count += 1
    return count


async def _all_episodes(provider, user_id: str, since_ts) -> List[Any]:
    """Paginate recall_episodes (provider clamps limit to 20) via until_ts."""
    if provider is None or not hasattr(provider, "recall_episodes"):
        return []
    out: List[Any] = []
    until_ts = None
    while len(out) < _EPISODE_MAX:
        try:
            page = await provider.recall_episodes(
                user_id=user_id, since_ts=since_ts, until_ts=until_ts,
                limit=_EPISODE_PAGE, order="newest")
        except Exception:
            break
        if not page:
            break
        out.extend(page)
        if len(page) < _EPISODE_PAGE:
            break
        until_ts = min(e.ts for e in page) - 1
    return out


async def _export_episodes(out: Path, provider, user_id: str, since_ts) -> int:
    episodes = await _all_episodes(provider, user_id, since_ts)
    by_day: Dict[str, List[Any]] = {}
    for e in episodes:
        by_day.setdefault(_fmt_day(e.ts) or "undated", []).append(e)
    for day, eps in by_day.items():
        lines = [f"# {day}", ""]
        for e in sorted(eps, key=lambda x: x.ts):
            lines.append(f"## {e.kind}: {e.task or '(no task)'}")
            lines.append(f"- outcome: **{e.outcome or '?'}** · steps: {e.steps or 0} "
                         f"· spend: ${float(e.spend_usd or 0):.4f} · session: `{e.session_id}`")
            if e.goal_id:
                lines.append(f"- goal: `{e.goal_id}`")
            if e.summary:
                lines.append(f"- summary: {e.summary}")
            arts = e.artifacts or []
            if arts:
                names = [a.get("path") or a.get("kind") or str(a) for a in arts
                         if isinstance(a, dict)] or [str(a) for a in arts]
                lines.append(f"- artifacts: {', '.join(names)}")
            lines.append("")
        _write(out / "episodes" / f"{day}.md", "\n".join(lines))
    return len(episodes)


def _export_skills(out: Path, user_id: str, data_dir: str) -> int:
    try:
        from agents.task.agent.skill_manager import get_skill_manager
        sm = get_skill_manager()
        catalog = sm.get_catalog_skills(user_id=user_id, max_skills=500)
    except Exception:
        return 0
    usage = {}
    try:
        from modules.skills.skill_usage import get_skill_usage_store
        usage = {r["skill_id"]: r for r in
                 get_skill_usage_store(data_dir).list_authored(user_id=user_id)}
    except Exception:
        usage = {}
    count = 0
    for m in catalog:
        try:
            body = m.content or sm._load_skill_content(m.skill_id, user_id=user_id) or ""
            u = usage.get(m.skill_id, {})
            front = _frontmatter({
                "skill_id": m.skill_id, "description": m.description or "",
                "created_by": u.get("created_by") or "", "source": m.source or "",
                "load_count": u.get("load_count", 0),
            })
            _write(out / "skills" / f"{sanitize_filename(m.skill_id)}.md", front + body.rstrip() + "\n")
            count += 1
        except Exception:
            continue
    return count


def _export_identity(out: Path, user_id: str, data_dir: str) -> int:
    count = 0
    try:
        from core.instance import load_self_context, load_self_doc, resolve_instance_id
        soul = load_self_context(data_dir)
        if soul:
            _write(out / "identity" / "soul.md", soul.rstrip() + "\n")
            count += 1
        self_doc = load_self_doc(data_dir, user_id, resolve_instance_id())
        if self_doc:
            _write(out / "identity" / "self.md", self_doc.rstrip() + "\n")
            count += 1
    except Exception:
        pass
    return count


def _export_goals(out: Path, user_id: str, data_dir: str) -> int:
    try:
        import os
        from agents.task.goals.board import GoalBoard
        board = GoalBoard(os.path.join(data_dir, "goals.db"))
        goals = board.list(user_id=user_id)
    except Exception:
        return 0
    if not goals:
        return 0
    lines = ["# Goal board", ""]
    for g in goals:
        lines.append(f"- **{g.title}** ({g.status}, p{g.priority}) — {g.body or ''}")
        if g.result:
            lines.append(f"  - result: {g.result}")
    _write(out / "goals.md", "\n".join(lines) + "\n")
    return len(goals)


async def build_vault(out_dir: str, *, user_id: str, data_dir: str,
                      provider=None, since_ts: Optional[int] = None) -> Dict[str, int]:
    """Write the vault; returns a manifest of per-section counts. Never raises."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"notes": 0, "episodes": 0, "skills": 0, "identity": 0, "goals": 0}
    try:
        manifest["notes"] = await _export_notes(out, provider, user_id)
    except Exception:
        logger.warning("vault: notes export failed", exc_info=True)
    try:
        manifest["episodes"] = await _export_episodes(out, provider, user_id, since_ts)
    except Exception:
        logger.warning("vault: episodes export failed", exc_info=True)
    manifest["skills"] = _export_skills(out, user_id, data_dir)
    manifest["identity"] = _export_identity(out, user_id, data_dir)
    manifest["goals"] = _export_goals(out, user_id, data_dir)

    day = time.strftime("%Y-%m-%d")
    idx = [
        f"# Knowledge vault — {user_id}", "",
        f"Exported {day} by `polyrob knowledge export`. The live DBs remain the "
        "source of truth; re-export to refresh.", "",
        f"- notes: {manifest['notes']} (see `notes/`)",
        f"- episodes: {manifest['episodes']} daily logs (see `episodes/`)",
        f"- skills: {manifest['skills']} (see `skills/`)",
        f"- identity docs: {manifest['identity']} (see `identity/`)",
        f"- goals: {manifest['goals']} (see `goals.md`)", "",
        "Open this folder as an Obsidian vault — note [[wikilinks]] and the "
        "graph view work out of the box.", "",
    ]
    _write(out / "index.md", "\n".join(idx))
    return manifest
