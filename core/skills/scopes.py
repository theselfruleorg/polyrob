"""Where skills live, and the ONE precedence order (081 WS-4.1).

Scopes:

- **builtin** — the bundled library. Read-only to the writer, trusted. Served from
  the SEEDED copy ``<data_home>/skills/bundled/`` once its manifest exists (081
  WS-5.3, :func:`bundled_seed_root`), else from the shipped package
  ``data/prompts/skills/`` (:func:`package_skills_root`, the seed source). A fix
  to a bundled skill is an owner-accepted change to the seeded copy, no deploy.
- **user** — ``<data_home>/skills/user_<uid>/``. Writable; data-home survives a
  ``polyrob update`` code-swap (the package tree does not).
- **pack:<id>** — a loaded pack's skills directory. Read-only, trusted.
- **project** / **user-external** — agentskills.io roots: the per-repo
  ``.agents/skills`` and ``.claude/skills`` walk (local operator only, see
  :func:`trust_project_skills_effective`), then ``~/.agents/skills`` and
  ``~/.claude/skills``. Loaded in place, never written.

Precedence, used by :func:`resolve` and nowhere else re-derived::

    builtin > user > pack > project > user-external

Builtin and pack ids are PROTECTED: an external skill under one is skipped, and a
pack skill under a builtin id is indexed as ``<pack>:<id>``. Two NON-protected
sources (user, project, user-external) that ship different bodies under one id
are ambiguous: neither is served, and the ambiguity is logged once (review C-9).
"""
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillScope:
    """One named root that skills can be read from / written to.

    ``trusted`` describes the LOCATION's provenance (pre-vetted shipped content
    vs. unreviewed content) — it is NOT a blanket "skip scanning" flag. See
    :func:`scan_exempt`, which additionally requires ``not writable``.
    """

    name: str
    root: Path
    writable: bool
    trusted: bool


#: The literal on-disk directory name under data-home.
_SKILLS_SUBDIR = "skills"


#: The seeded bundled library under ``<data_home>/skills`` and its ``id:sha256``
#: manifest. The manifest is written last by the seed, so its presence means a
#: complete copy; until then the package serves.
BUNDLED_DIRNAME = "bundled"
SEED_MANIFEST = ".bundled_manifest"


def package_skills_root() -> Path:
    """The installed package's shipped skills directory: ``<root>/data/prompts/skills``
    (``core/skills/<this file>`` -> parents[2] is the repo / site-packages root).
    The seed source; the deploy syncs it with ``--delete``."""
    return Path(__file__).resolve().parents[2] / "data" / "prompts" / "skills"


def bundled_seed_root() -> Path:
    """``<data_home>/skills/bundled`` — the seeded copy of the bundled library."""
    return skills_data_home() / BUNDLED_DIRNAME


def read_seed_manifest(root: Path) -> Dict[str, str]:
    """``{skill_id: origin_sha256}`` from ``root``'s manifest (``{}`` when absent).
    Raises on an unreadable manifest: an unreadable store is not an empty one."""
    path = Path(root) / SEED_MANIFEST
    if not path.is_file():
        return {}
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        sid, sep, sha = line.strip().partition(":")
        if sep and sid:
            out[sid] = sha.strip()
    return out


def _builtin_root() -> Path:
    """The root the builtin scope serves: the seeded copy once seeded, else the package."""
    try:
        seeded = bundled_seed_root()
        if (seeded / SEED_MANIFEST).is_file():
            return seeded
    except Exception:
        logger.debug("seeded skill root unavailable; serving the package", exc_info=True)
    return package_skills_root()


def _data_home() -> Path:
    """``<data_home>`` with the local-vs-server split applied — the ONE rule
    worker storage and the update snapshot/rollback paths also resolve through."""
    from core.runtime_paths import effective_data_home
    return effective_data_home()


def skills_data_home() -> Path:
    """``<data_home>/skills`` — the writable root for user-authored skills."""
    return _data_home() / _SKILLS_SUBDIR


def builtin_scope() -> SkillScope:
    """The bundled, read-only, pre-vetted system-skill library (the seeded copy once
    seeded). The package root may also hold legacy ``user_<uid>/`` dirs until the
    migration moves them; enumerate it with :func:`builtin_skill_ids`, never with a
    bare ``iterdir``."""
    return SkillScope(name="builtin", root=_builtin_root(), writable=False, trusted=True)


def user_scope() -> SkillScope:
    """The writable, tenant-scoped, data-home skill root (survives updates)."""
    return SkillScope(name="user", root=skills_data_home(), writable=True, trusted=True)


def pack_scopes() -> List[SkillScope]:
    """067 P2: one read-only, trusted ``pack:<id>`` scope per LOADED pack that
    ships a skills directory. A pack's skills exist only while it is loaded."""
    from core.packs.state import skill_dirs
    return [SkillScope(name=f"pack:{pack_id}", root=root, writable=False, trusted=True)
            for pack_id, root in skill_dirs()]


def scan_exempt(scope: SkillScope) -> bool:
    """Whether content resident in ``scope`` may skip a write-time threat re-scan.
    Only a read-only + trusted scope; a writable scope is NEVER exempt."""
    return scope.trusted and not scope.writable


def builtin_skill_ids(scope: Optional[SkillScope] = None) -> List[str]:
    """Sorted ids of the skills under ``scope`` (default :func:`builtin_scope`),
    EXCLUDING ``user_*`` and dotted directories."""
    scope = scope or builtin_scope()
    root = scope.root
    if not root.is_dir():
        return []
    return sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith((".", "user_")) and (d / "SKILL.md").exists()
    )


# --- external (agentskills.io) discovery ---------------------------------------

#: Dot-prefixed dirs (.git, .venv, .pending, .archived) are skipped by the walk.
_SCAN_SKIP = {"node_modules", "__pycache__", "site-packages"}


@dataclass(frozen=True)
class DiscoveredSkill:
    skill_id: str          # frontmatter name if present, else dir name
    scope: str             # "user" | "project" | "pack:<id>"
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

    SERVER (not local mode): OFF, unconditionally — the process CWD is the install
    dir, not a trusted operator repo. LOCAL operator: default ON (the CWD is the
    operator's own repo); opt out with POLYROB_TRUST_PROJECT_SKILLS=false."""
    from core.config_policy import _bool_env, local_mode_enabled
    if not local_mode_enabled():
        return False
    return _bool_env("POLYROB_TRUST_PROJECT_SKILLS", True)


def discover_skills(root: Path, scope: str, *, max_depth: int = 4,
                    max_count: int = 2000) -> List[DiscoveredSkill]:
    """Lenient load of every SKILL.md under ``root``: warn and load; skip only on an
    error-level issue. ``max_count`` bounds the directories visited."""
    from core.skills.frontmatter import parse_frontmatter, validate_consumed
    out: List[DiscoveredSkill] = []
    try:
        root = root.resolve()
    except OSError:
        return out
    if not root.is_dir():
        return out
    root_depth = len(root.parts)
    stack = [root]
    visited = 0
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


# --- the external index ---------------------------------------------------------

#: (skill_id, path, path) ambiguities already logged in this process.
_AMBIGUITY_LOGGED: set = set()


def _log_ambiguity_once(skill_id: str, first, second) -> None:
    key = (skill_id, str(first), str(second))
    if key in _AMBIGUITY_LOGGED:
        return
    _AMBIGUITY_LOGGED.add(key)
    logger.warning("skill collision: %r is ambiguous (%s vs %s) — neither is loaded; "
                   "rename one of them", skill_id, first, second)


def _index_pack_skills(index: Dict[str, DiscoveredSkill], protected_ids: set) -> None:
    """067 P2: loaded packs' skills, ahead of every external root. A pack skill whose
    id is taken (a protected builtin, or an earlier pack) is indexed as ``<pack>:<skill>``."""
    import dataclasses
    try:
        scopes = pack_scopes()
    except Exception:
        logger.warning("pack skill scopes unavailable", exc_info=True)
        return
    for scope in scopes:
        pack_id = scope.name.split(":", 1)[1]
        for ds in discover_skills(scope.root, scope.name):
            if ds.skill_id in protected_ids or ds.skill_id in index:
                new_id = f"{pack_id}:{ds.skill_id}"
                logger.info("pack skill %r from %s indexed as %r (id already taken)",
                            ds.skill_id, scope.name, new_id)
                ds = dataclasses.replace(ds, skill_id=new_id)
            index[ds.skill_id] = ds


def external_index() -> Dict[str, DiscoveredSkill]:
    """Every pack, project and user-external skill, keyed by the id it is served under.

    Builtin ids are protected (an external skill under one is skipped with a
    warning). Pack ids are protected the same way against project/user roots. Two
    project/user-external skills with one id and DIFFERENT bodies are ambiguous:
    both are dropped (logged once). An identical copy (the same skill synced into
    two roots) is not ambiguous; the first root keeps it.
    """
    try:
        protected_ids = set(builtin_skill_ids())
    except Exception:
        logger.debug("builtin-id lookup skipped (non-fatal)", exc_info=True)
        protected_ids = set()
    index: Dict[str, DiscoveredSkill] = {}
    _index_pack_skills(index, protected_ids)
    roots = []
    try:
        if trust_project_skills_effective():
            roots += [("project", r) for r in project_external_roots()]
    except Exception:
        logger.debug("project-skill discovery skipped", exc_info=True)
    roots += [("user", r) for r in user_external_roots()]
    ambiguous: set = set()
    for scope, root in roots:
        for ds in discover_skills(root, scope):
            sid = ds.skill_id
            if sid in protected_ids:
                logger.warning("skill collision: %r in %s shadowed by protected builtin id %r",
                               sid, ds.path, sid)
                continue
            prior = index.get(sid)
            if prior is not None and prior.scope.startswith("pack:"):
                logger.warning("skill collision: %r in %s shadowed by pack skill %s",
                               sid, ds.path, prior.path)
                continue
            if sid in ambiguous:
                continue
            if prior is not None:
                if prior.body != ds.body:
                    _log_ambiguity_once(sid, prior.path, ds.path)
                    ambiguous.add(sid)
                    index.pop(sid, None)
                continue
            index[sid] = ds
    return index


# --- the ONE precedence function -----------------------------------------------

@dataclass(frozen=True)
class Resolved:
    """Where a skill id is served from."""
    scope: str           # "builtin" | "user" | "pack:<id>" | "project" | "user-external"
    path: Path           # the skill directory
    external: Optional[DiscoveredSkill] = None


def resolve(skill_id: str, user_id: Optional[str] = None, *,
            builtin_root: Optional[Path] = None,
            users_root: Optional[Path] = None,
            external: Optional[Dict[str, DiscoveredSkill]] = None) -> Optional[Resolved]:
    """The skill ``skill_id`` serves, or ``None``: builtin > user > pack > project > user-external.

    ``builtin_root`` / ``users_root`` / ``external`` default to the live roots; a
    caller that redirected its roots (a test, a tenant sandbox) passes its own. A
    tenant skill that shares its id with a project / user-external skill is
    ambiguous: neither is served, and the ambiguity is logged once.
    """
    if (not skill_id or "/" in skill_id or "\\" in skill_id or ".." in skill_id
            or skill_id != skill_id.strip()):
        return None
    root = builtin_root if builtin_root is not None else _builtin_root()
    try:
        if (root / skill_id / "SKILL.md").is_file():
            return Resolved("builtin", root / skill_id)
    except OSError:
        pass
    user_dir = None
    if user_id:
        base = users_root if users_root is not None else skills_data_home()
        cand = base / f"user_{user_id}" / skill_id
        try:
            if (cand / "SKILL.md").is_file():
                user_dir = cand
        except OSError:
            user_dir = None
    ext = (external if external is not None else external_index()).get(skill_id)
    if user_dir is not None:
        if ext is not None and not ext.scope.startswith("pack:"):
            _log_ambiguity_once(skill_id, user_dir, ext.path)
            return None
        return Resolved("user", user_dir)
    if ext is not None:
        scope = ext.scope if ext.scope.startswith("pack:") or ext.scope == "project" \
            else "user-external"
        return Resolved(scope, ext.path, ext)
    return None
