"""Pure discovery of external agentskills.io skills (~/.agents/skills, ~/.claude/skills,
and per-repo project dirs). Lenient-load: warn+load; skip only on an error-level Issue.
No writes, no rules.json mutation — the in-memory rule is synthesized by SkillManager."""
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

# Dot-prefixed dirs (.git, .venv, .pending, .archived, ...) are all covered by the
# startswith(".") check below the walk uses — only list non-dot names here to avoid
# double-covering the same skip with two mechanisms.
_SCAN_SKIP = {"node_modules", "__pycache__", "site-packages"}


@dataclass(frozen=True)
class DiscoveredSkill:
    skill_id: str          # frontmatter name if present, else dir name
    scope: str             # "user" | "project"
    path: Path             # the skill directory (contains SKILL.md)
    meta: dict             # parsed frontmatter
    body: str              # frontmatter-stripped body
    warnings: tuple = ()   # warn-level Issue codes (for logging only)


def user_external_roots() -> List[Path]:
    home = Path.home()
    return [home / ".agents" / "skills", home / ".claude" / "skills"]


def project_external_roots() -> List[Path]:
    """CWD->git-root walk collecting per-repo skill dirs (most-local first). Bounded to 64 levels."""
    roots = []
    cur = Path.cwd().resolve()
    for _ in range(64):
        for rel in (".agents/skills", ".claude/skills"):
            cand = cur / rel
            if cand.is_dir():
                roots.append(cand)
        if (cur / ".git").exists():
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return roots


def trust_project_skills_effective() -> bool:
    """Project-scope discovery gate.

    SERVER (not local_mode): fail-closed OFF, unconditionally — the process CWD is the
    install dir, NOT a trusted operator repo (mirrors PROJECT_CONTEXT_SERVER_MODE). No
    env can flip it on here; a server that wants project skills must ship them in the
    tenant workspace via a future server-mode path, never scan CWD.
    LOCAL operator: default ON (the CWD is the operator's own repo); explicit opt-out via
    POLYROB_TRUST_PROJECT_SKILLS=false."""
    from agents.task import constants
    if not constants.local_mode_enabled():
        return False
    from agents.task.constants import _bool_env
    return _bool_env("POLYROB_TRUST_PROJECT_SKILLS", True)


def discover_skills(root: Path, scope: str, *, max_depth: int = 4, max_count: int = 2000) -> List[DiscoveredSkill]:
    from .skill_frontmatter import parse_frontmatter
    from .skill_validation import validate_consumed
    out: List[DiscoveredSkill] = []
    try:
        root = root.resolve()
    except OSError:
        return out
    if not root.is_dir():
        return out
    root_depth = len(root.parts)
    stack = [root]
    visited = 0  # counts every directory popped/processed, so max_count bounds total
                 # work (breadth), not just the number of skills matched
    while stack and visited < max_count:
        d = stack.pop()
        visited += 1
        md = d / "SKILL.md"
        if md.is_file():                       # a skill dir is a LEAF — never descend into its resources
            try:
                raw = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta, body = parse_frontmatter(raw)
            issues = validate_consumed(meta, d.name)
            errs = [i.code for i in issues if i.level == "error"]
            if errs:
                logger.warning("skill discovery: skipping %s (%s)", d, ",".join(errs))
                continue
            out.append(DiscoveredSkill(
                skill_id=str(meta.get("name") or d.name),
                scope=scope, path=d, meta=meta, body=body,
                warnings=tuple(i.code for i in issues if i.level == "warn"),
            ))
            continue
        if len(d.parts) - root_depth >= max_depth:
            continue
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_dir() and e.name not in _SCAN_SKIP and not e.name.startswith("."):
                stack.append(e)
    return out
