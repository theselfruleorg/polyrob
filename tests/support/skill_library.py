"""Build a throwaway skill library for a test (081 WS-4.2).

A skill's SKILL.md frontmatter is its only metadata source, so a test that needs
a rule writes the rule INTO the frontmatter, the way a shipped skill does.
"""
import json
from pathlib import Path
from typing import Dict, Optional

from core.skills.frontmatter import emit_frontmatter, encode_triggers, parse_frontmatter


def skill_md(skill_id: str, rule: Optional[dict] = None, body: str = "") -> str:
    """The SKILL.md text for ``skill_id``: ``body`` (which may carry its own
    frontmatter) with ``rule`` written as ``metadata.polyrob-*``."""
    meta, text = parse_frontmatter(body) if body.lstrip().startswith("---") else ({}, body)
    meta = dict(meta)
    meta.setdefault("name", skill_id)
    md = dict(meta.get("metadata") or {})
    if rule is not None:
        if rule.get("description") and not meta.get("description"):
            meta["description"] = rule["description"]
        md["polyrob-triggers"] = encode_triggers(rule.get("triggers") or {})
        if "priority" in rule:
            md["polyrob-priority"] = str(rule["priority"])
        md["polyrob-auto-activate"] = "true" if rule.get("auto_activate", True) else "false"
        if rule.get("requires"):
            md["polyrob-requires"] = json.dumps(list(rule["requires"]), separators=(",", ":"))
    if md:
        meta["metadata"] = md
    meta.setdefault("description", f"{skill_id} skill")
    return emit_frontmatter(meta) + (text or f"# {skill_id}\n\nA test skill body for {skill_id}.\n")


def write_library(root: Path, rules: Dict[str, dict],
                  bodies: Optional[Dict[str, str]] = None) -> Path:
    """Write ``root/<id>/SKILL.md`` for every id in ``rules`` or ``bodies``."""
    root = Path(root)
    bodies = bodies or {}
    for sid in list(rules) + [b for b in bodies if b not in rules]:
        d = root / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(skill_md(sid, rules.get(sid), bodies.get(sid, "")),
                                    encoding="utf-8")
    return root
