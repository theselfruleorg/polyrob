"""
Skill Manager - Loads and matches skills for each session.

Skills are context-aware prompt extensions that get loaded into a session's
system prompt based on:
1. Which tools are loaded for the session (tool_ids)
2. Keywords in the task description
3. Available actions

SYSTEM (builtin) skills are stored in the shipped package tree:
data/prompts/skills/ (see skill_store.builtin_scope()). Per-tenant
user_<uid> skills are stored under DATA-HOME (skill_store.skills_data_home(),
Task 8) so they survive a `polyrob update` code-swap, not under this package
tree — see SkillManager._user_dirs_root().
"""

import json
import re
import logging
import hashlib
from pathlib import Path
from typing import List, Dict, Optional, Set, Any, Tuple
from dataclasses import dataclass, field

from agents.task.agent.skill_frontmatter import parse_frontmatter
from agents.task.agent import skill_store

logger = logging.getLogger(__name__)

# Validation constants
# Task 7: split the single flat char cap into two thresholds. The old
# MAX_SKILL_CONTENT_CHARS=12000 was a HARD reject in validate_skill_content (the
# gate used by SkillWriterMixin.create_skill and by import/validation paths),
# which silently rejected spec-valid agentskills.io skills well within the
# ~5000-token injected-body recommendation - e.g. the real anthropics/skills
# `docx` skill (20084 chars) and `skill-creator` (33168 chars) both bust the old
# 12000 cap, so the import path could not ingest most real-world skills.
MAX_SKILL_FILE_CHARS = 40000      # on-disk ceiling (DoS guard) - was implicitly 12000
MAX_SKILL_INJECT_CHARS = 20000    # ~5000 tok recommended injected-body size (agentskills.io)
SOFT_INJECT_WARN_CHARS = MAX_SKILL_INJECT_CHARS  # warn-only threshold at injection time
MAX_SKILL_CONTENT_CHARS = MAX_SKILL_FILE_CHARS   # back-compat alias (no code imports this as of 2026-07 grep)
MIN_SKILL_CONTENT_CHARS = 50     # Minimum meaningful content
MAX_SKILL_ID_LENGTH = 50
# DERIVED from the WS-2 capability table (was a third hand-list that went stale on
# optional/posture tools — T5). Every registrable tool must have a capability row
# (register_optional_tool refuses otherwise), so the table's keys ARE the registrable
# vocabulary; `tool_manage` is the one aspirational id (gated everywhere, not yet
# registrable). Registry parity stays belt-and-braces guarded by
# tests/unit/agents/task/test_valid_tool_ids_parity.py.
from core.tool_capabilities import classified_ids as _classified_ids

# `worker_manage` (041) is a controller ACTION with a capability row, not a container tool.
VALID_TOOL_IDS = set(_classified_ids()) - {'tool_manage', 'worker_manage'}


def validate_skill_content_length(body: str) -> Tuple[bool, str]:
    """Pure length check for a skill body (Task 7).

    Encodes the ONE hard-reject window: [MIN_SKILL_CONTENT_CHARS, MAX_SKILL_FILE_CHARS].
    A body between MAX_SKILL_INJECT_CHARS and MAX_SKILL_FILE_CHARS is ACCEPTED here -
    it may still land on disk / be read via a skill resource - callers that inject the
    body into the prompt should separately warn (not reject) past MAX_SKILL_INJECT_CHARS;
    see the warning emitted in format_skills_for_prompt().
    """
    n = len(body)
    if n < MIN_SKILL_CONTENT_CHARS:
        return False, f"too short ({n}<{MIN_SKILL_CONTENT_CHARS})"
    if n > MAX_SKILL_FILE_CHARS:
        return False, f"too large ({n}>{MAX_SKILL_FILE_CHARS})"
    return True, ""


def parse_skill_frontmatter(content: str) -> Tuple[Dict[str, str], str]:
    """Split an optional leading YAML frontmatter block from a skill body (P1-1).

    Supports the agentskills.io / clawhub open standard where a SKILL.md opens with::

        ---
        name: my-skill
        description: ...
        ---
        # Heading ...

    Returns ``(frontmatter_dict, body)``. With no frontmatter, returns ``({}, content)``.
    Parsing is intentionally dependency-free (simple ``key: value`` lines) so it never
    pulls in a YAML lib; unparseable lines are skipped, not fatal.
    """
    if not content:
        return {}, content or ""
    text = content.lstrip('﻿')  # tolerate a leading BOM
    if not text.startswith('---'):
        return {}, content
    lines = text.splitlines()
    # first line is the opening '---'; find the closing fence
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            fm: Dict[str, str] = {}
            for raw in lines[1:i]:
                if ':' in raw:
                    k, _, v = raw.partition(':')
                    key = k.strip()
                    if key:
                        fm[key] = v.strip().strip('"').strip("'")
            body = '\n'.join(lines[i + 1:]).lstrip('\n')
            return fm, body
    return {}, content  # no closing fence -> treat as plain content


def strip_skill_frontmatter(content: str) -> str:
    """Return the skill body with any leading YAML frontmatter removed (P1-1)."""
    return parse_skill_frontmatter(content)[1]


@dataclass
class SkillValidationResult:
    """Result of skill validation."""
    skill_id: str
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class MatchedSkill:
    """A skill that matched the session context."""
    skill_id: str
    priority: int
    match_reasons: List[str]
    content: str
    description: str = ""
    trigger_type: str = "auto"  # Primary trigger type that matched
    # Task 14: provenance for externally-discovered (agentskills.io ecosystem) skills —
    # "" for builtin/user, "user"/"project" for ~/.agents/skills etc. Purely informational.
    source: str = ""

    def __getitem__(self, key: str):
        """Dict-style access (``m["id"]``) alongside normal attribute access, so
        catalog consumers can treat every entry uniformly regardless of whether
        it originated from rules.json or from external skill discovery."""
        if key == "id":
            return self.skill_id
        return getattr(self, key)


@dataclass
class SkillContext:
    """Context for skill matching - represents a session's configuration."""
    tool_ids: List[str] = field(default_factory=list)
    task: str = ""
    available_actions: List[str] = field(default_factory=list)


from agents.task.agent.skill_writer import SkillWriterMixin


class SkillManager(SkillWriterMixin):
    """
    Manages skill loading and matching for sessions.
    
    Each session calls get_skills_for_session() during initialization.
    The returned skills are embedded into the system prompt.
    
    Usage:
        skill_manager = SkillManager()
        
        # During session initialization:
        matched_skills = skill_manager.get_skills_for_session(
            tool_ids=['browser', 'mcp'],
            task="research LinkedIn profiles and create report"
        )
        
        # Format for prompt embedding:
        skill_content = skill_manager.format_skills_for_prompt(matched_skills)
    """
    
    def __init__(self, skills_dir: Optional[Path] = None):
        """Initialize SkillManager with skills directory.

        Args:
            skills_dir: Path to the BUILTIN (system) skills directory. Defaults
                to the shipped package's ``data/prompts/skills/`` (Task 8:
                ``skill_store.builtin_scope().root``). Per-tenant
                ``user_<uid>`` reads/writes do NOT use this by default — see
                ``_user_dirs_root()``, which routes them to the writable
                data-home scope instead so they survive a ``polyrob update``
                code-swap. Passing this explicitly (as several existing tests
                do, to isolate BOTH builtin rules.json AND user writes under
                one tmp root) also redirects user dirs here, preserving that
                pre-existing single-root override contract.
        """
        # Recorded once so `_user_dirs_root()` can detect an override of the
        # TRUE builtin default — whether via this constructor arg or a later
        # direct `.skills_dir =` mutation (both patterns are used by the
        # existing test suite for single-root isolation).
        self._builtin_default_dir = skill_store.builtin_scope().root
        if skills_dir:
            self.skills_dir = Path(skills_dir)
        else:
            self.skills_dir = self._builtin_default_dir

        self.skill_rules: Dict[str, dict] = {}
        self.skill_cache: Dict[str, str] = {}
        # Parsed YAML frontmatter (agentskills.io) for each loaded skill, keyed by the
        # same cache_key as skill_cache. Populated by _load_skill_content, consumed by
        # _get_skill_meta() (e.g. catalog description preference in _resolve_skill_description).
        self.skill_meta_cache: Dict[str, Dict[str, Any]] = {}
        # 067 P6: a skill's `allowed-tools` declaration (advisory, see
        # skill_allowed_tools.py), keyed like skill_cache.
        self.skill_allowed_tools: Dict[str, List[str]] = {}
        self._rules_loaded = False
        # Task 14: lazy, once-per-instance cache of externally-discovered
        # (~/.agents/skills, ~/.claude/skills, ...) skills. None = not yet computed;
        # recomputed on next access after reload_rules() invalidates it.
        self._external_index: Optional[Dict[str, "skill_discovery.DiscoveredSkill"]] = None

        # Task 9: one-time migration of any pre-Task-8 code-tree user_<uid>/
        # skills into data-home, so an existing deployment doesn't strand them
        # the moment this class starts reading user skills from data-home
        # instead of the package tree (see skill_store.py's migration section
        # docstring). ALWAYS targets the REAL package builtin tree
        # (self._builtin_default_dir), never a test's skills_dir override -
        # migration is about where legacy skills physically shipped, not
        # wherever a given SkillManager instance has been redirected to.
        # Guarded: fail-open (never blocks construction), and the migration
        # itself is idempotent/locked/resumable (see migrate_legacy_user_skills).
        try:
            _moved = skill_store.migrate_legacy_user_skills(self._builtin_default_dir)
            if _moved:
                logger.info("migrated %d legacy user skill(s) into data-home", _moved)
        except Exception:
            logger.debug("legacy skill migration skipped (non-fatal)", exc_info=True)

        logger.debug(f"SkillManager initialized with skills_dir: {self.skills_dir}")

    def _load_external_skills(self) -> Dict[str, Any]:
        """Discover + precedence-dedupe external (agentskills.io ecosystem) skills.

        Project roots (higher precedence) are consulted ONLY when
        ``skill_discovery.trust_project_skills_effective()`` says so (Task 15 —
        that function does not exist yet, so this is guarded and inert until
        then); user roots (``~/.agents/skills``, ``~/.claude/skills``) are
        always consulted. Cached once per instance; call ``reload_rules()`` to
        force a re-scan.
        """
        if self._external_index is not None:
            return self._external_index
        from . import skill_discovery
        # Task 16: builtin ids are PROTECTED — an external (ecosystem) skill can
        # never enter the index under a builtin's name, no matter its scope, so
        # it can never shadow the builtin body nor appear in its place in the
        # catalog. Fail-open: if this lookup errors, treat as "no protected ids"
        # rather than blocking external discovery.
        try:
            protected_ids = set(skill_store.builtin_skill_ids())
        except Exception:
            logger.debug("builtin-id lookup skipped (non-fatal)", exc_info=True)
            protected_ids = set()
        index: Dict[str, Any] = {}
        self._index_pack_skills(index, protected_ids, skill_discovery)
        roots = []
        try:
            if skill_discovery.trust_project_skills_effective():
                roots += [("project", r) for r in skill_discovery.project_external_roots()]
        except Exception:
            logger.debug("project-skill discovery skipped", exc_info=True)
        roots += [("user", r) for r in skill_discovery.user_external_roots()]
        for scope, root in roots:                              # project-first => higher precedence
            for ds in skill_discovery.discover_skills(root, scope):
                if ds.skill_id in protected_ids:
                    logger.warning("skill collision: %r in %s shadowed by protected builtin id %r",
                                   ds.skill_id, ds.path, ds.skill_id)
                    continue
                if ds.skill_id in index:
                    logger.warning("skill collision: %r in %s shadowed by higher-precedence %s",
                                   ds.skill_id, ds.path, index[ds.skill_id].path)
                    continue
                index[ds.skill_id] = ds
        self._external_index = index
        return index

    @staticmethod
    def _index_pack_skills(index: Dict[str, Any], protected_ids: set, skill_discovery) -> None:
        """067 P2: loaded packs' skills, ahead of every external root (precedence
        user > builtin > pack > external). A pack skill whose id is taken (a
        protected builtin, or an earlier pack) is indexed as ``<pack>:<skill>``."""
        import dataclasses
        try:
            scopes = skill_store.pack_scopes()
        except Exception:
            logger.warning("pack skill scopes unavailable", exc_info=True)
            return
        for scope in scopes:
            pack_id = scope.name.split(":", 1)[1]
            for ds in skill_discovery.discover_skills(scope.root, scope.name):
                if ds.skill_id in protected_ids or ds.skill_id in index:
                    new_id = f"{pack_id}:{ds.skill_id}"
                    logger.info("pack skill %r from %s indexed as %r (id already taken)",
                                ds.skill_id, scope.name, new_id)
                    ds = dataclasses.replace(ds, skill_id=new_id)
                index[ds.skill_id] = ds

    def _ensure_rules_loaded(self) -> None:
        """Load skill rules from rules.json if not already loaded."""
        if self._rules_loaded:
            return
        
        rules_file = self.skills_dir / "rules.json"
        if rules_file.exists():
            try:
                with open(rules_file, 'r') as f:
                    self.skill_rules = json.load(f)
                logger.info(f"Loaded {len(self.skill_rules)} skill rules from {rules_file}")
            except Exception as e:
                logger.error(f"Failed to load skill rules: {e}")
                self.skill_rules = {}
        else:
            logger.warning(f"No skill rules file found at {rules_file}")
            self.skill_rules = {}

        # 067 P3: a loaded pack's skills ship their rules next to them
        # (``<pack>/skills/rules.json``) and join the builtin rule set here, so a
        # pack skill auto-activates exactly like the builtin it used to be.
        try:
            self._merge_pack_rules()
        except Exception:  # pragma: no cover - a pack must never block the rules
            logger.warning("pack skill rules skipped", exc_info=True)

        # P0-2: drift guard. A rule with auto_activate but no on-disk SKILL.md body is a
        # SILENT failure (matches, then drops with only a WARNING). Prune such orphans
        # from the in-memory rules so they can never burn match work or mislead the
        # catalog. Fail-open — a content gap must never block boot.
        try:
            self._prune_bodiless_rules()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"orphan-rule prune skipped (non-fatal): {e}")

        # Task 4: fail-open boot warning for strict agentskills.io frontmatter
        # compliance. Observability only — a non-compliant skill (or the check
        # itself erroring) must NEVER block startup.
        try:
            bad = self.validate_all_authored()
            if bad:
                logger.warning("skill compliance: %d non-compliant skill(s): %s",
                               len(bad), ", ".join(sorted(bad)))
        except Exception:
            logger.debug("skill compliance check skipped", exc_info=True)

        self._rules_loaded = True

    def _merge_pack_rules(self) -> None:
        """Add each loaded pack's ``skills/rules.json`` rows (067 P3) under the id
        the pack skill is indexed as (``<pack>:<id>`` on a collision). Only for the
        builtin rule set (a manager pointed at another directory is a test or a
        tenant sandbox); a row never overrides an existing one."""
        self._pack_rule_ids = set()
        if Path(self.skills_dir) != Path(self._builtin_default_dir):
            return
        scopes = skill_store.pack_scopes()
        if not scopes:
            return
        index = self._load_external_skills()
        for scope in scopes:
            rules_file = Path(scope.root) / "rules.json"
            if not rules_file.is_file():
                continue
            rows = json.loads(rules_file.read_text(encoding="utf-8"))
            if not isinstance(rows, dict):
                logger.warning("pack rules %s: not an object — skipped", rules_file)
                continue
            pack_id = scope.name.split(":", 1)[1]
            for sid, rule in rows.items():
                indexed = sid if getattr(index.get(sid), "scope", None) == scope.name \
                    else f"{pack_id}:{sid}"
                ds = index.get(indexed)
                if ds is None or ds.scope != scope.name or not isinstance(rule, dict):
                    continue  # no body shipped in this pack
                if indexed in self.skill_rules:
                    continue
                self.skill_rules[indexed] = rule
                self._pack_rule_ids.add(indexed)

    def _prune_bodiless_rules(self) -> None:
        """Drop any auto_activate system rule whose SKILL.md body is missing."""
        pack_ids = getattr(self, "_pack_rule_ids", set())
        orphans = [
            sid for sid, rules in self.skill_rules.items()
            if rules.get("auto_activate", True)
            and sid not in pack_ids
            and not (self.skills_dir / sid / "SKILL.md").exists()
        ]
        if not orphans:
            return
        try:
            from agents.task.constants import local_mode_enabled
            severe = local_mode_enabled()
        except Exception:
            severe = False
        msg = (f"Skill rules reference {len(orphans)} auto_activate id(s) with no "
               f"SKILL.md body — pruning so they can't silent-drop: {orphans}")
        (logger.error if severe else logger.warning)(msg)
        for sid in orphans:
            self.skill_rules.pop(sid, None)
    
    def reserved_skill_ids(self) -> Set[str]:
        """Ids a USER skill may never take: every shipped builtin skill, every
        loaded pack skill, and every system rule id (review 2026-09-29 D1).

        A user skill under a builtin id used to REPLACE the builtin — its body,
        its triggers, its ``requires`` and its ``auto_activate: false`` gate
        (``skill_manage create secret-handling`` -> promote). The REST create
        route already refused with 409; the writer and the installer did not.
        Fail-open per source: an unreadable pack list never blocks the builtin
        check.
        """
        ids: Set[str] = set(getattr(self, "skill_rules", None) or {})
        ids |= set(getattr(self, "_pack_rule_ids", None) or ())
        try:
            ids |= set(skill_store.builtin_skill_ids())
        except Exception:
            logger.debug("builtin-id lookup skipped", exc_info=True)
        try:
            if Path(self.skills_dir) != Path(self._builtin_default_dir):
                ids |= set(skill_store.builtin_skill_ids(skill_store.SkillScope(
                    name="builtin", root=Path(self.skills_dir),
                    writable=False, trusted=True)))
        except Exception:
            logger.debug("custom builtin-id lookup skipped", exc_info=True)
        try:
            for scope in skill_store.pack_scopes():
                ids |= set(skill_store.builtin_skill_ids(scope))
        except Exception:
            logger.debug("pack-id lookup skipped", exc_info=True)
        return ids

    def reserved_skill_id_error(self, skill_id: str) -> Optional[str]:
        """The refusal text when ``skill_id`` is reserved, else ``None``."""
        if skill_id in self.reserved_skill_ids():
            return (f"skill id '{skill_id}' belongs to a builtin or pack skill; a user "
                    f"skill cannot replace it — choose a different id")
        return None

    def _merge_user_rules(self, all_rules: dict, user_rules: Any,
                          user_id: Optional[str] = None) -> dict:
        """Apply a tenant's rules over the system rules, IN PLACE, and return them.

        * D10: a non-dict rules file or a non-dict entry is skipped with a
          warning. One malformed row used to raise ``AttributeError`` inside
          ``get_skills_for_session`` and the session silently got NO skills.
        * D1: a user rule under a SYSTEM id (a legacy shadow written before the
          writer refused builtin ids) may only DISABLE the system skill
          (``auto_activate: false``). It never replaces the system triggers,
          ``requires``, priority or gate.
        """
        if not isinstance(user_rules, dict):
            if user_rules:
                logger.warning("user skill rules for %s are not an object — ignored", user_id)
            return all_rules
        system = getattr(self, "skill_rules", None) or {}
        for sid, rule in user_rules.items():
            if not isinstance(rule, dict):
                logger.warning("user skill rule %r for %s is not an object — skipped",
                               sid, user_id)
                continue
            if sid in system and isinstance(system.get(sid), dict):
                if rule.get("auto_activate", True) is False:
                    merged = dict(system[sid])
                    merged["auto_activate"] = False
                    all_rules[sid] = merged
                else:
                    logger.debug("user rule %r shadows a system skill — ignored", sid)
                continue
            all_rules[sid] = rule
        return all_rules

    @staticmethod
    def _money_tool_gate_ok(triggers: dict, session_tool_ids) -> bool:
        """False when a money playbook's session cannot reach its domain.

        The session must hold a declared tool that is either a MONEY tool
        (``defi_trade``) or a pure read tool of the domain (``defi_data`` — no
        ``high_impact`` capability; a session that can look at a market still
        benefits from the screens). A declared ACTION tool that moves no funds —
        ``cronjob`` on ``dca``/``exits`` — does not count: a cron-only session
        cannot execute a DCA buy, so the doctrine stays out (review 2026-09-29
        D2; the old ``set(declared) & set(session)`` let ``cronjob`` open it).

        Reads ``core.tool_capabilities`` rather than a hardcoded list, so a new
        money tool is covered the day it is classified. Fail-OPEN: if the
        capability table cannot be read, surface the skill as before — losing a
        playbook is worse than showing one too often.
        """
        declared = list((triggers or {}).get("tool_ids") or [])
        if not declared:
            return True
        try:
            from core.tool_capabilities import ids_with
            money = ids_with("money")
            acting = ids_with("high_impact")
        except Exception:
            return True
        if not any(t in money for t in declared):
            return True
        reach = {t for t in declared if t in money or t not in acting}
        return bool(reach & set(session_tool_ids or []))

    #: D7: the task characters trigger keywords and patterns are matched on.
    MAX_TRIGGER_TASK_CHARS = 4000

    #: 068 A1: the most prerequisite skills one session may pull in beyond
    #: ``max_skills``. Bounded because every injected body costs prompt.
    MAX_PREREQUISITES = 2

    def _with_prerequisites(self, result: List["MatchedSkill"], all_rules: dict,
                            tool_ids, user_id) -> List["MatchedSkill"]:
        """Append the ``requires`` skills of every loaded skill, once each.

        A rule may declare ``"requires": [skill_id, ...]`` in rules.json. The
        2026-09-25 wrong-token buy ran with the memecoin playbook loaded and the
        identity procedure absent: "buy PNL" matched the playbook, and nothing
        pulled in the step that says "resolve the address first". A prerequisite
        loads even when the cap cut it, but only when its own money-tool gate
        passes for this session, and at most ``MAX_PREREQUISITES`` are added.

        Semantics (068 N5):

        * ``result`` is the FINAL loaded set — trigger matches AND seeded skills —
          so a seeded skill's ``requires`` apply exactly like a matched one's.
        * ``all_rules`` is the effective rule set (system rules with the user's
          overrides applied). A prerequisite whose effective rule says
          ``auto_activate: false`` is NOT pulled in: a user who disabled a skill
          disabled it, and ``requires`` never re-enables it. A skill the user
          SEEDS explicitly is still loaded — seeding is explicit intent.
        * One pass, no recursion: a prerequisite's own ``requires`` are not
          expanded. Bounded by ``MAX_PREREQUISITES``.
        """
        present = {m.skill_id for m in result}
        extra: List[MatchedSkill] = []
        for m in list(result):
            rule = all_rules.get(m.skill_id)
            if not isinstance(rule, dict):
                continue
            for req in rule.get("requires") or []:
                if len(extra) >= self.MAX_PREREQUISITES:
                    return result + extra
                if not isinstance(req, str) or req in present:
                    continue
                req_rule = all_rules.get(req)
                if not isinstance(req_rule, dict):
                    continue
                if req_rule.get("auto_activate", True) is False:
                    continue  # disabled (by the user or the system) stays disabled
                if not self._money_tool_gate_ok(req_rule.get("triggers", {}), tool_ids):
                    continue
                content = self._load_skill_content(req, user_id=user_id)
                if not content:
                    continue
                extra.append(MatchedSkill(
                    skill_id=req,
                    priority=req_rule.get("priority", 5),
                    match_reasons=[f"requires:{m.skill_id}"],
                    content=content,
                    description=self._resolve_skill_description(req, req_rule, user_id=user_id),
                    trigger_type="prerequisite",
                ))
                present.add(req)
        return result + extra

    def get_skills_for_session(
        self,
        tool_ids: Optional[List[str]] = None,
        task: str = "",
        available_actions: Optional[List[str]] = None,
        max_skills: int = 2,  # Reduced from 5 to prevent prompt bloat
        user_id: Optional[str] = None,
        seeded_skill_ids: Optional[List[str]] = None,
    ) -> List[MatchedSkill]:
        """
        Get skills that match the session's configuration.
        
        This is the main entry point - call during session initialization.
        
        Args:
            tool_ids: List of tools loaded for this session (e.g., ['browser', 'mcp'])
            task: The task description for this session
            available_actions: List of available action names
            max_skills: Maximum number of skills to return
            user_id: Optional user ID to include user's custom skills
            
        Returns:
            List of MatchedSkill objects sorted by priority
        """
        self._ensure_rules_loaded()
        
        tool_ids = tool_ids or []
        available_actions = available_actions or []
        # D7: triggers read the task's head only. The patterns are regexes with
        # `.*` between two alternations — quadratic on a 46 KB pasted task
        # (8.8 s, synchronous at construction). What a task is ABOUT is in its
        # first few thousand characters.
        task = (task or "")[:self.MAX_TRIGGER_TASK_CHARS]
        task_lower = task.lower()

        # Combine system + user rules
        all_rules = dict(self.skill_rules)
        user_skills_dir = None

        if user_id:
            user_rules, user_skills_dir = self._load_user_rules(user_id)
            self._merge_user_rules(all_rules, user_rules, user_id)
        
        matches = []
        
        for skill_id, rules in all_rules.items():
            if not rules.get("auto_activate", True):
                continue

            triggers = rules.get("triggers", {})

            # A playbook for MOVING FUNDS is not surfaced to a session that
            # cannot move funds. Measured on prod 2026-09-12: with `defi_trade`
            # absent from the toolset, "buy a token" still pinned 517 lines of
            # execution doctrine — a sizing ladder, screens and exit rules for a
            # rail the agent structurally could not reach. Reading an execution
            # playbook you cannot execute is the setup for telling the owner a
            # shipped capability does not exist.
            #
            # Deliberately narrow: only a skill DECLARING a money tool is gated,
            # and a declared money tool or pure read tool (not an action tool
            # like `cronjob`) satisfies it — so a read-only `defi_data` session
            # still gets the screens. It stays in the trigger path (rather than
            # `auto_activate: false`) so a GRANTED run gets the doctrine
            # delivered: eager bodies, or, under progressive disclosure,
            # `split_progressive` pins what fits EAGER_INJECT_BUDGET_CHARS
            # (prerequisites first) and marks the rest LOAD FIRST in the catalog
            # (review 2026-09-29 D5 — "PINNED" used to be false under defaults).
            if not self._money_tool_gate_ok(triggers, tool_ids):
                continue

            # Collect matches by type
            tool_matches = []
            keyword_matches = []
            pattern_matches = []
            action_matches = []

            # Check tool ID matching
            trigger_tool_ids = triggers.get("tool_ids", [])
            for tool_id in trigger_tool_ids:
                if tool_id in tool_ids:
                    tool_matches.append(f"tool:{tool_id}")

            # Check keyword matching in task. P2-19: WORD-BOUNDARY match, not a raw
            # substring — a short keyword like "sell"/"buy"/"trade"/"plan"/"fix" used
            # to fire on "counselling"/"busy"/"airplane"/"prefix" substrings, and a
            # priority-1 false positive could evict a genuinely relevant skill under the
            # max_skills cap. re.escape handles punctuation; a multi-word keyword is
            # matched as a phrase bounded at each end.
            keywords = triggers.get("keywords", [])
            for kw in keywords:
                kw_l = (kw or "").lower().strip()
                if not kw_l:
                    continue
                # P2-19 (Fusion follow-up): `\b` only asserts a boundary NEXT TO a word
                # char, so a keyword that starts or ends with a non-word char ("c++",
                # ".net") gets NO boundary there and `\b<kw>\b` never matches — a silent
                # false-negative the re.error fallback (zero matches, not an error) can't
                # catch. Anchor `\b` conditionally: only at an end whose adjacent keyword
                # char is a word char. So "sell" -> `\bsell\b` (fixes the "counselling"
                # false positive) but "c++" -> `\bc\+\+` (still matches "use c++ here").
                def _wordish(ch: str) -> bool:
                    return ch.isalnum() or ch == "_"
                left = r"\b" if _wordish(kw_l[0]) else ""
                right = r"\b" if _wordish(kw_l[-1]) else ""
                try:
                    if re.search(left + re.escape(kw_l) + right, task_lower):
                        keyword_matches.append(f"keyword:{kw}")
                except re.error:
                    # Degenerate keyword -> fall back to substring (never crash matching)
                    if kw_l in task_lower:
                        keyword_matches.append(f"keyword:{kw}")

            # Check action name matching
            action_names = triggers.get("action_names", [])
            for action in action_names:
                if action in available_actions:
                    action_matches.append(f"action:{action}")

            # Check task pattern matching (regex)
            task_patterns = triggers.get("task_patterns", [])
            for pattern in task_patterns:
                try:
                    if re.search(pattern, task, re.IGNORECASE):
                        pattern_matches.append(f"pattern:{pattern}")
                except re.error:
                    pass  # Invalid regex, skip

            # CRITICAL: Determine if skill should load based on RELEVANCE to task
            # - Keyword or pattern match = task is relevant → LOAD
            # - Tool match ONLY (no keyword/pattern) = tool present but task not relevant → SKIP
            # - Tool match + keyword/pattern = strongly relevant → LOAD
            has_task_relevance = bool(keyword_matches or pattern_matches)
            has_tool_match = bool(tool_matches)

            # Only load if task is actually relevant (keyword/pattern match required)
            # Exception: if skill has no tool_ids defined, action match alone can trigger
            trigger_tool_ids_defined = bool(triggers.get("tool_ids", []))

            should_load = False
            if has_task_relevance:
                # Task mentions relevant keywords/patterns - load this skill
                should_load = True
            elif not trigger_tool_ids_defined and action_matches:
                # Skill doesn't require specific tools and has matching actions
                should_load = True
            # Note: tool_match alone is NOT enough - prevents loading person-analyzer
            # just because MCP is present when task is about presentations

            if should_load:
                match_reasons = tool_matches + keyword_matches + pattern_matches + action_matches
                content = self._load_skill_content(skill_id, user_id=user_id)
                if content:
                    # Determine primary trigger type
                    trigger_type = "auto"
                    if any(r.startswith("tool:") for r in match_reasons):
                        trigger_type = "tool"
                    elif any(r.startswith("keyword:") for r in match_reasons):
                        trigger_type = "keyword"
                    elif any(r.startswith("action:") for r in match_reasons):
                        trigger_type = "action"
                    elif any(r.startswith("pattern:") for r in match_reasons):
                        trigger_type = "pattern"
                    
                    matches.append(MatchedSkill(
                        skill_id=skill_id,
                        priority=rules.get("priority", 5),
                        match_reasons=match_reasons,
                        content=content,
                        description=self._resolve_skill_description(skill_id, rules, user_id=user_id),
                        trigger_type=trigger_type
                    ))
        
        # Sort by priority (lower = higher priority), then by number of matches
        matches.sort(key=lambda m: (m.priority, -len(m.match_reasons)))
        
        # 068 N4: a procedure that another MATCHED skill ``requires`` does not
        # compete for a capped slot with the skill the task asked for — it is
        # appended by `_with_prerequisites` below. "Buy ETH every week" used to
        # load the memecoin doctrine + token-identity and cut `dca`, the one
        # skill the owner requested. A prerequisite the task names directly
        # (and no matched skill requires) keeps its normal rank.
        #
        # 068 R3-5: a prerequisite is demoted ONLY when a parent that requires
        # it is itself SELECTED (survives the cap) and so will pull it back in.
        # Computing the demotion from every match let a parent that was later
        # cut still evict its prerequisite — identity vanished although the
        # doctrine requiring it never loaded. Fixed point over the selection,
        # bounded (each pass is a pure function of the previous selection).
        def _requires_of(ids):
            return {req for sid in ids
                    for req in ((all_rules.get(sid) or {}).get("requires") or [])
                    if isinstance(req, str) and req != sid}

        covered = _requires_of(m.skill_id for m in matches)
        result = []
        converged = False
        for _ in range(4):
            primary = [m for m in matches if m.skill_id not in covered]
            secondary = [m for m in matches if m.skill_id in covered]
            result = (primary + secondary)[:max_skills]
            selected_parents = {m.skill_id for m in result}
            next_covered = _requires_of(selected_parents) - selected_parents
            if next_covered == covered:
                converged = True
                break
            covered = next_covered
        if not converged:
            # 068 R4-3: a dependency graph that does not settle in the bound
            # falls back to plain priority order — no demotion at all — so a
            # high-priority prerequisite can never vanish because the passes
            # oscillated between parents that do not survive the cap.
            result = matches[:max_skills]

        # Force-include preset-seeded skills regardless of trigger match.
        # Seeds bypass max_skills truncation (they are explicit user intent).
        if seeded_skill_ids:
            present = {m.skill_id for m in result}
            for sid in seeded_skill_ids:
                if sid in present:
                    continue  # already in result (via trigger match) → dedup
                rules = all_rules.get(sid)
                if not rules:
                    continue  # unknown id → skip (fail-open)
                content = self._load_skill_content(sid, user_id=user_id)
                if not content:
                    continue  # no SKILL.md → skip (fail-open)
                result.append(MatchedSkill(
                    skill_id=sid,
                    priority=rules.get("priority", 99),
                    match_reasons=["seeded:preset"],
                    content=content,
                    description=self._resolve_skill_description(sid, rules, user_id=user_id),
                    trigger_type="seeded",
                ))

        # 068 A1/N5: every loaded skill — matched or seeded — pulls in the
        # procedures it assumes.
        result = self._with_prerequisites(result, all_rules, tool_ids, user_id)

        if result:
            logger.info(
                f"Matched {len(result)} skills for session: "
                f"{[m.skill_id for m in result]}"
            )
        elif task_lower.strip():
            # P2-1b: a non-trivial task that trigger-matched nothing is a SILENT recall
            # miss. Make it observable. (With catalog-include-all default-ON the agent can
            # still discover these via load_skill, but the trigger miss itself is signal.)
            available = sorted(
                sid for sid, r in all_rules.items() if r.get("auto_activate", True)
            )
            if available:
                logger.info(
                    "no skills matched the task; %d available but un-surfaced by triggers: %s",
                    len(available), available,
                )

        return result

    #: D8: catalog rank of an external (ecosystem) skill — after builtin (1-5)
    #: and agent-authored (6) skills.
    EXTERNAL_SKILL_PRIORITY = 9

    def get_catalog_skills(
        self,
        user_id: Optional[str] = None,
        max_skills: int = 20,
        tool_ids: Optional[List[str]] = None,
    ) -> List["MatchedSkill"]:
        """Return auto-activatable skills as catalog entries (S-1, true progressive disclosure).

        Unlike :meth:`get_skills_for_session` (which trigger-matches against the task),
        this exposes every available skill so the agent can DISCOVER and ``load_skill``
        any of them on demand — fixing the gap where a session that matched zero
        triggers got an empty catalog and could load nothing. Bodies are preloaded so
        ``load_skill`` serves without a disk re-read. Sorted by priority, capped.

        P1-1: a GATED skill (``auto_activate: false`` — e.g. the money/trading
        playbooks) is surfaced ONLY when the session has loaded its required tools
        (``triggers.tool_ids`` all present in *tool_ids*) — that is what those triggers
        were written for. Otherwise it stays hidden AND the load_skill fallback refuses
        it (see :meth:`may_load_skill`), so the gate is real, not advisory.

        067 P0.1: in a SESSION context (*tool_ids* given) an auto-activatable money
        playbook (e.g. ``treasury-trading``) passes the same money-tool gate as the
        trigger path (:meth:`_money_tool_gate_ok`) — otherwise the catalog listed a
        playbook the trigger path had just withheld. ``tool_ids=None`` is a listing
        view (console, export, ``/skills``), not a session, and lists it.
        """
        self._ensure_rules_loaded()
        all_rules = dict(self.skill_rules)
        if user_id:
            user_rules, _ = self._load_user_rules(user_id)
            self._merge_user_rules(all_rules, user_rules, user_id)

        session_tool_ids = set(tool_ids or [])
        catalog = []
        # 067 P4: ids a gate below withheld. A pack skill (e.g. the markets pack's
        # polymarket-trading) is ALSO discovered as an external skill, so the append
        # further down must not put a withheld one back.
        withheld = set()
        for skill_id, rules in all_rules.items():
            if not rules.get("auto_activate", True):
                # P1-1: surface a gated skill only when its required tools are loaded.
                gate_tool_ids = set(rules.get("triggers", {}).get("tool_ids", []))
                if not (gate_tool_ids and gate_tool_ids.issubset(session_tool_ids)):
                    withheld.add(skill_id)
                    continue
            elif tool_ids is not None and not self._money_tool_gate_ok(
                    rules.get("triggers", {}), session_tool_ids):
                withheld.add(skill_id)
                continue
            content = self._load_skill_content(skill_id, user_id=user_id)
            if not content:
                continue
            catalog.append(MatchedSkill(
                skill_id=skill_id,
                priority=rules.get("priority", 5),
                match_reasons=["catalog"],
                content=content,
                description=self._resolve_skill_description(skill_id, rules, user_id=user_id),
                trigger_type="catalog",
            ))

        # Task 14: append externally-discovered (agentskills.io ecosystem) skills that
        # aren't already covered by a builtin/user rule of the same id.
        # P1-7: external skills (from ~/.agents/skills, ~/.claude/skills) are untrusted
        # third-party content that gets pinned into EVERY session's catalog prompt —
        # scan the description AND body, and skip any that trips the injection scan
        # (fail-OPEN if the scanner is unavailable, fail-CLOSED if it raises), matching
        # the writer's P3-1 stance that a catalog description is an injection vector.
        existing_ids = {m.skill_id for m in catalog}
        for ext_id, ds in self._load_external_skills().items():
            if ext_id in existing_ids or ext_id in withheld:
                continue
            ext_desc = ds.meta.get("description", "")
            # A pack skill is trusted shipped content (scan-exempt like builtin).
            if not ds.scope.startswith("pack:") and self._external_content_suspicious(
                    ext_id, ext_desc, ds.body, user_id=user_id):
                continue
            # D8: an ecosystem skill (~/.claude/skills, ~/.agents/skills) ranks
            # AFTER every builtin (1-5) and agent-authored (6) skill, so a laptop
            # with dozens of third-party skills cannot crowd the library out of
            # the capped catalog. A pack skill with no rule keeps the builtin band.
            catalog.append(MatchedSkill(
                skill_id=ext_id,
                priority=5 if ds.scope.startswith("pack:") else self.EXTERNAL_SKILL_PRIORITY,
                match_reasons=["catalog"],
                content=ds.body,
                description=ext_desc,
                trigger_type="catalog",
                source=ds.scope,
            ))
            existing_ids.add(ext_id)

        catalog.sort(key=lambda m: (m.priority, m.skill_id))
        return catalog[:max_skills]

    @staticmethod
    def _external_content_suspicious(skill_id: str, description: str, body: str,
                                     *, user_id: Optional[str] = None) -> bool:
        """True if an external skill's description/body trips the injection scan (P1-7).

        Fail-OPEN when the scanner can't be imported (parity with the rest of the
        codebase); fail-CLOSED (treat as suspicious) when the scanner itself raises.

        ``user_id`` is the TENANT the threat report is filed under (045 I5) — a
        hit written with no tenant is readable by no owner seat at all.
        """
        try:
            from modules.memory.task.threat_scan import is_suspicious
        except Exception:
            return False  # scanner unavailable → fail-open
        try:
            combined = f"{description}\n\n{body}"
            if is_suspicious(combined):
                logger.warning(
                    "external skill %r excluded from catalog: content tripped the "
                    "injection scan", skill_id,
                )
                from core.security.threat_report import report_threat
                report_threat("skill", source="skill_manager", detail=skill_id,
                              user_id=user_id or "")
                return True
            return False
        except Exception:
            logger.warning(
                "external skill %r excluded from catalog: injection scan raised "
                "(fail-closed)", skill_id,
            )
            return True

    def _user_dirs_root(self) -> Path:
        """Root under which per-tenant ``user_<uid>`` skill directories live (Task 8).

        Defaults to the WRITABLE data-home user scope
        (``skill_store.skills_data_home()``) so a create/patch/delete survives
        a ``polyrob update`` code-tree swap — the installed package tree
        (``self.skills_dir``'s default) is replaced wholesale on update, but
        data-home is not.

        If ``skills_dir`` has been redirected away from the true builtin
        default — via the constructor arg or by mutating ``.skills_dir``
        directly (the single-root isolation pattern several existing tests
        use) — that redirected root governs user dirs too, so those tests
        keep working unchanged. This is a deliberate, dynamically-re-checked
        comparison (not a flag frozen at ``__init__`` time) so a POST-construction
        mutation of ``.skills_dir`` (as `test_skill_overwrite_protect.py` does)
        is honored too.
        """
        if self.skills_dir != self._builtin_default_dir:
            return self.skills_dir
        return skill_store.skills_data_home()

    def resolve_skill_dir(self, skill_id: str, user_id: Optional[str] = None) -> Optional[Path]:
        """Resolve a skill's on-disk directory for read-only resource access (Task 17).

        Precedence: builtin (``skills_dir/<skill_id>``, if it has a
        ``SKILL.md`` — matches ``_load_skill_content``'s lookup, so a
        ``SkillManager(skills_dir=custom)`` test-isolation override resolves
        consistently here too) > per-tenant user dir
        (``_user_dirs_root()/user_<uid>/<skill_id>``, if it exists) >
        externally-discovered (``~/.agents/skills``, ``~/.claude/skills``,
        ...) skill's ``.path``. Returns ``None`` if the skill can't be located on disk
        (e.g. an in-memory-only / test-injected skill) — fail-open, never raises.

        Guards against path escape up front: a falsy/empty, multi-segment
        (contains ``/`` or ``\\``), parent-traversing (``..``), or
        whitespace-padded ``skill_id`` returns ``None`` immediately. This is
        deliberately looser than ``validate_skill_id`` — it must still accept
        legit lenient external ids (digit-leading, unicode, e.g.
        ``3d-modeling``), just refuse anything that could escape the
        directory join below.

        Reused by Task 18 (list/read wiring) — do not duplicate this lookup elsewhere.
        """
        if (
            not skill_id
            or "/" in skill_id
            or "\\" in skill_id
            or ".." in skill_id
            or skill_id != skill_id.strip()
        ):
            return None
        try:
            # Use self.skills_dir (not self._builtin_default_dir) so a
            # SkillManager(skills_dir=custom) test-isolation override is
            # honored here too, matching _load_skill_content's lookup.
            builtin_dir = self.skills_dir / skill_id
            if (builtin_dir / "SKILL.md").exists():
                return builtin_dir
        except OSError:
            pass
        if user_id:
            try:
                user_dir = self._user_root(user_id) / skill_id
                self._read_skill_text(user_dir / "SKILL.md")
                return user_dir
            except (OSError, ValueError):
                pass
        try:
            ext = self._load_external_skills().get(skill_id)
            if ext is not None:
                return ext.path
        except Exception:
            logger.debug("resolve_skill_dir: external skill lookup failed for %s", skill_id, exc_info=True)
        return None

    def _load_user_rules(self, user_id: str) -> tuple:
        """Load user's custom skill rules.

        Args:
            user_id: User identifier

        Returns:
            Tuple of (rules dict, user skills directory Path)
        """
        if self._require_user(user_id) is None:
            return {}, None
        user_dir = self._user_root(user_id)
        rules_file = user_dir / "rules.json"
        
        if rules_file.exists():
            try:
                rules = json.loads(self._read_skill_text(rules_file, max_bytes=1_048_576))
                if not isinstance(rules, dict):
                    logger.warning("user skill rules for %s are not an object — ignored",
                                   user_id)
                    return {}, user_dir
                logger.debug(f"Loaded {len(rules)} user skill rules for {user_id}")
                return rules, user_dir
            except Exception as e:
                logger.warning(f"Failed to load user rules for {user_id}: {e}")
        
        return {}, user_dir

    def get_skill_rule(self, skill_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        """Return the effective rule dict for a skill_id, or None.

        A user rule under a system id may only disable it (see
        :meth:`_merge_user_rules`); any other user id is the user's own rule."""
        self._ensure_rules_loaded()
        if user_id:
            user_rules, _ = self._load_user_rules(user_id)
            if isinstance(user_rules, dict) and skill_id in user_rules:
                merged = self._merge_user_rules({}, {skill_id: user_rules[skill_id]}, user_id)
                if skill_id in merged:
                    return merged[skill_id]
        return self.skill_rules.get(skill_id)

    def may_load_skill(
        self, skill_id: str, tool_ids: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> bool:
        """Gate for on-demand loading (P1-1).

        An ``auto_activate: false`` skill (e.g. the money/trading playbooks) is a GATED
        skill: it may be loaded ONLY when the session has loaded its required tools
        (``triggers.tool_ids`` all present). Otherwise the gate would only hide the
        skill from the catalog while the load_skill disk fallback served the full
        playbook to any model that guessed the id. An unknown id / any auto_activate
        skill is loadable (True) — the fallback's own tenant/path guards still apply.

        067 P0.1: an auto_activate money playbook is loadable only when it passes
        :meth:`_money_tool_gate_ok` — the same gate the trigger path and the
        session catalog apply, so listing and loading agree.
        """
        rule = self.get_skill_rule(skill_id, user_id=user_id)
        if rule is None:
            return True  # unknown to rules.json — not a gated skill; other guards apply
        if rule.get("auto_activate", True):
            return self._money_tool_gate_ok(rule.get("triggers", {}), tool_ids or [])
        gate_tool_ids = set(rule.get("triggers", {}).get("tool_ids", []))
        return bool(gate_tool_ids and gate_tool_ids.issubset(set(tool_ids or [])))

    def _load_skill_content(self, skill_id: str, user_id: Optional[str] = None) -> str:
        """Load skill markdown content from disk, stripped of any YAML frontmatter.

        SKILL.md may open with an agentskills.io-style frontmatter block
        (``---\\nname: ...\\n---``). That block is metadata for tooling, not
        instructions for the LLM, so it is parsed off here — the single source
        point — and cached separately (see ``_get_skill_meta``); only the
        frontmatter-free body is cached/returned. Every consumer of the returned
        content (format_skills_for_prompt, build_load_skill_result,
        validate_skill_content's content_len/estimated_tokens) therefore sees a
        clean body without having to strip anything itself.

        Args:
            skill_id: The skill identifier (directory name)
            user_id: Optional user ID to check user skills first

        Returns:
            Skill content as string (frontmatter stripped), or empty string if not found
        """
        if (not skill_id or "/" in skill_id or "\\" in skill_id or ".." in skill_id
                or skill_id != skill_id.strip()):
            return ""
        if user_id is not None and self._require_user(user_id) is None:
            return ""
        # Create cache key that includes user context
        cache_key = f"{user_id}:{skill_id}" if user_id else skill_id

        # D9: ONE precedence order, shared with ``resolve_skill_dir``:
        # builtin (``skills_dir``) > per-tenant user > external. It used to be
        # user-first here and builtin-first there, so a shadowed id served the
        # user's body next to the builtin's references/. The writer refuses a
        # builtin id (D1), so a user skill under one is a legacy leftover.
        skill_file = self.skills_dir / skill_id / "SKILL.md"
        if skill_file.exists():
            if cache_key in self.skill_cache:
                return self.skill_cache[cache_key]
            try:
                raw = skill_file.read_text(encoding='utf-8')
                meta, body = parse_frontmatter(raw)
                self.skill_meta_cache[cache_key] = meta
                body = self._note_allowed_tools(cache_key, skill_id, meta, body)
                self.skill_cache[cache_key] = body
                logger.debug(f"Loaded skill content for '{skill_id}' ({len(body)} chars)")
                return body
            except Exception as e:
                logger.error(f"Failed to load skill content for '{skill_id}': {e}")

        # Versioned user skills are hash-verified below before every load. Do
        # not serve an old process-local cache after another worker atomically
        # changes SKILL.md/rules.json; legacy skills retain the original cache
        # behaviour until their next write backfills a revision.
        expected_content_hash = None
        if user_id:
            try:
                # Do not use _load_user_rules here: that compatibility helper
                # deliberately turns a malformed rules file into {}. For a
                # versioned instruction source, treating broken metadata as
                # "no hash" would silently disable the integrity check.
                rules_file = self._user_root(user_id) / "rules.json"
                if rules_file.exists():
                    user_rules = json.loads(self._read_skill_text(rules_file, max_bytes=1_048_576))
                    if not isinstance(user_rules, dict):
                        raise ValueError("user rules must be an object")
                else:
                    user_rules = {}
                rule = user_rules.get(skill_id, {})
                if isinstance(rule, dict):
                    expected_content_hash = rule.get("content_sha256")
                if expected_content_hash is not None and (
                        not isinstance(expected_content_hash, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", expected_content_hash)):
                    logger.warning("refusing malformed revision metadata for user skill %s", skill_id)
                    return ""
            except Exception:
                # A versioned skill must not load when its verification metadata
                # cannot be read. Legacy user skills preserve the old fail-open
                # rules behaviour because they contain no revision field.
                logger.warning("refusing user skill %s: could not read revision metadata", skill_id)
                return ""
        if cache_key in self.skill_cache and expected_content_hash is None:
            return self.skill_cache[cache_key]

        # Then the tenant's own skills
        if user_id:
            user_skill_file = self._user_root(user_id) / skill_id / "SKILL.md"
            if user_skill_file.exists():
                try:
                    raw = self._read_skill_text(user_skill_file)
                    if expected_content_hash is not None:
                        actual = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                        if actual != expected_content_hash:
                            logger.warning("refusing mismatched user skill body for '%s'", skill_id)
                            return ""
                    meta, body = parse_frontmatter(raw)
                    self.skill_meta_cache[cache_key] = meta
                    body = self._note_allowed_tools(cache_key, skill_id, meta, body)
                    self.skill_cache[cache_key] = body
                    logger.debug(f"Loaded user skill content for '{skill_id}' ({len(body)} chars)")
                    return body
                except Exception as e:
                    logger.error(f"Failed to load user skill content for '{skill_id}': {e}")

        # Task 14: external (~/.agents/skills, ~/.claude/skills, ...) lookup — LAST,
        # so builtin/user skills always take precedence over ecosystem ones.
        ext = self._load_external_skills().get(skill_id)
        if ext is not None:
            self.skill_meta_cache[cache_key] = ext.meta
            body = self._note_allowed_tools(cache_key, skill_id, ext.meta, ext.body)
            self.skill_cache[cache_key] = body
            logger.debug(f"Loaded external ({ext.scope}) skill content for '{skill_id}' ({len(body)} chars)")
            return body

        logger.warning(f"Skill file not found: {skill_file}")
        return ""

    def _note_allowed_tools(self, cache_key: str, skill_id: str, meta: Dict[str, Any],
                            body: str) -> str:
        """067 P6: record a declared `allowed-tools` list and append the ADVISORY
        line to the body (not enforced — no per-turn narrowing hook exists)."""
        from agents.task.agent.skill_allowed_tools import apply
        body, tools = apply(meta, body, skill_id=skill_id)
        store = self.__dict__.setdefault("skill_allowed_tools", {})
        if tools:
            store[cache_key] = tools
        else:
            store.pop(cache_key, None)
        return body

    def _get_skill_meta(self, skill_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Return the parsed YAML frontmatter for a skill (populated as a side effect
        of ``_load_skill_content``). Empty dict if the skill has no frontmatter, was
        not found, or has not been loaded yet in this process.
        """
        cache_key = f"{user_id}:{skill_id}" if user_id else skill_id
        return self.skill_meta_cache.get(cache_key, {})

    def _resolve_skill_description(self, skill_id: str, rules: dict,
                                    user_id: Optional[str] = None) -> str:
        """Resolve the catalog-facing description for a skill.

        The SKILL.md frontmatter ``description`` is the agentskills.io-compliant
        source of truth and is preferred when present (non-empty); the rules.json
        ``description`` is the fallback (pre-frontmatter behavior, and the only
        option for skills / callers with no parsed frontmatter). Callers MUST call
        ``_load_skill_content(skill_id, user_id=user_id)`` first so the frontmatter
        cache is populated. This does not affect gating — auto_activate/priority/
        triggers keep reading from ``rules`` untouched.
        """
        meta = self._get_skill_meta(skill_id, user_id=user_id)
        return meta.get("description") or rules.get("description", "")
    
    def format_skills_for_prompt(
        self,
        matched_skills: List[MatchedSkill],
        include_metadata: bool = True
    ) -> str:
        """
        Format matched skills for embedding into system prompt with XML tags.

        Args:
            matched_skills: List of MatchedSkill objects
            include_metadata: Whether to include match reasons

        Returns:
            Formatted string with XML-tagged skills ready for prompt embedding
        """
        if not matched_skills:
            return ""

        sections = []
        sections.append("<skills>")
        sections.append("These are recommended workflows for this task. Prefer them when "
                         "they apply, and use the tools they specify.")
        sections.append("Use judgment: if a step is impossible or a specified tool is "
                         "unavailable, adapt and use the best available alternative.")
        sections.append("")

        for skill in matched_skills:
            # Task 7: warn (never reject) when a matched skill's body is bigger than
            # the recommended injected-body size - the on-disk file already passed
            # the (higher) MAX_SKILL_FILE_CHARS hard reject in validate_skill_content;
            # this is just visibility that a specific skill is bloating the prompt.
            if len(skill.content) > SOFT_INJECT_WARN_CHARS:
                logger.warning(
                    "skill '%s' body is %d chars (> %d recommended inject size) - "
                    "injecting in full anyway; consider SKILL_PROGRESSIVE_DISCLOSURE "
                    "or trimming the skill",
                    skill.skill_id, len(skill.content), SOFT_INJECT_WARN_CHARS,
                )
            sections.append(f'<skill id="{skill.skill_id}">')

            if include_metadata and skill.match_reasons:
                # Group match reasons by type
                keyword_matches = [r for r in skill.match_reasons if r.startswith("keyword:")]

                if keyword_matches:
                    sections.append(f"⚡ Relevant to: {', '.join(r.split(':')[1] for r in keyword_matches[:3])}")
                sections.append("")

            sections.append(skill.content)
            sections.append("</skill>")
            sections.append("")

        sections.append("</skills>")
        return "\n".join(sections)
    
    def format_skill_catalog(self, matched_skills: List[MatchedSkill],
                             load_first: Optional[Set[str]] = None) -> str:
        """Format matched skills as a compact catalog (S-1 progressive disclosure).

        Lists only id + one-line description per skill (~20-30 tok each) instead of
        the full bodies. The agent loads a skill's full instructions on demand via
        the load_skill(skill_id) tool. Returns "" when nothing matched.

        ``load_first`` (D5): ids that matched THIS task but did not fit the eager
        budget. They are listed first with a ``LOAD FIRST`` mark, so the one
        instruction to load them is visible without reading their body.
        """
        if not matched_skills:
            return ""
        load_first = set(load_first or ())

        lines = [
            "<skill-catalog>",
            f"{len(matched_skills)} skill(s) are available for this task. Each is a "
            "detailed workflow you should follow when relevant.",
            "Call load_skill(skill_id=\"<id>\") to load a skill's FULL instructions "
            "BEFORE doing the work it covers. Load only what the current step needs.",
        ]
        if load_first:
            lines.append("Entries marked LOAD FIRST matched this task: load each one "
                         "before you act on the task.")
        lines.append("")
        ordered = ([s for s in matched_skills if s.skill_id in load_first]
                   + [s for s in matched_skills if s.skill_id not in load_first])
        for skill in ordered:
            desc = (skill.description or skill.skill_id.replace("-", " ")).strip()
            # Keep each line short — one sentence of description at most.
            desc = desc.splitlines()[0][:160] if desc else skill.skill_id
            mark = "LOAD FIRST — " if skill.skill_id in load_first else ""
            lines.append(f'- id="{skill.skill_id}" — {mark}{desc}')
        lines.append("</skill-catalog>")
        return "\n".join(lines)

    #: D5: under progressive disclosure, the most body characters eager-injected
    #: for the skills that MATCHED the session (prerequisites first). ~5k tokens.
    #: A skill that does not fit is marked LOAD FIRST in the catalog instead.
    EAGER_INJECT_BUDGET_CHARS = MAX_SKILL_INJECT_CHARS

    #: pinning order under the budget: safety prerequisites, then rail/persona
    #: seeds, then trigger matches (already in priority order).
    _EAGER_ORDER = {"prerequisite": 0, "seeded": 1}

    def split_progressive(self, session_matched: List[MatchedSkill],
                          budget: Optional[int] = None
                          ) -> Tuple[List[MatchedSkill], Set[str]]:
        """Split the session's MATCHED skills into (eager, load_first) — D5.

        Under the defaults (progressive disclosure ON, catalog include-all ON)
        a trigger match, a rail seed and a ``requires`` prerequisite used to
        only reorder the catalog: nothing reached the model unless it chose to
        call ``load_skill``. Now every matched body that fits the budget is
        pinned in full, prerequisites first; the rest are catalog entries
        marked LOAD FIRST. Bounded: at most ``budget`` characters of bodies.
        """
        remaining = self.EAGER_INJECT_BUDGET_CHARS if budget is None else int(budget)
        ordered = sorted(
            enumerate(session_matched or []),
            key=lambda p: (self._EAGER_ORDER.get(p[1].trigger_type, 2), p[0]))
        eager: List[MatchedSkill] = []
        load_first: Set[str] = set()
        for _, skill in ordered:
            size = len(skill.content or "")
            if 0 < size <= remaining:
                eager.append(skill)
                remaining -= size
            else:
                load_first.add(skill.skill_id)
        return eager, load_first

    def format_progressive(self, eager: List[MatchedSkill],
                           catalog: List[MatchedSkill],
                           load_first: Optional[Set[str]] = None) -> str:
        """The pinned skill message under progressive disclosure (D5): the eager
        bodies, then the catalog of everything else."""
        eager_ids = {s.skill_id for s in eager}
        parts = []
        if eager:
            parts.append(self.format_skills_for_prompt(eager))
        rest = [s for s in catalog if s.skill_id not in eager_ids]
        if rest:
            parts.append(self.format_skill_catalog(rest, load_first=load_first))
        return "\n\n".join(parts)

    def get_skill_ids(self) -> List[str]:
        """Get list of all available skill IDs.
        
        Returns:
            List of skill identifiers
        """
        self._ensure_rules_loaded()
        return list(self.skill_rules.keys())
    
    def reload_rules(self) -> None:
        """Force reload of skill rules from disk."""
        self._rules_loaded = False
        self.skill_cache.clear()
        self.skill_meta_cache.clear()
        self.skill_allowed_tools.clear()
        self._external_index = None  # Task 14: force external re-scan too
        self._ensure_rules_loaded()
        logger.info("Skill rules reloaded")

    def validate_skill_id(self, skill_id: str) -> Tuple[bool, List[str]]:
        """Validate skill ID format.

        Args:
            skill_id: The skill identifier to validate

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        if not skill_id:
            errors.append("Skill ID cannot be empty")
            return False, errors

        if len(skill_id) > MAX_SKILL_ID_LENGTH:
            errors.append(f"Skill ID exceeds max length of {MAX_SKILL_ID_LENGTH}")

        if not re.match(r'^[a-z][a-z0-9-]*$', skill_id):
            errors.append("Skill ID must be lowercase, start with letter, contain only letters, numbers, hyphens")

        if '--' in skill_id:
            errors.append("Skill ID cannot contain consecutive hyphens")

        if skill_id.endswith('-'):
            errors.append("Skill ID cannot end with hyphen")

        return len(errors) == 0, errors

    def validate_skill_rules(self, skill_id: str, rules: dict) -> SkillValidationResult:
        """Validate a skill's rules configuration.

        Args:
            skill_id: The skill identifier
            rules: The rules dict from rules.json

        Returns:
            SkillValidationResult with validation details
        """
        errors = []
        warnings = []

        # Validate skill ID
        id_valid, id_errors = self.validate_skill_id(skill_id)
        errors.extend(id_errors)

        # Validate triggers
        triggers = rules.get("triggers", {})

        # Validate tool_ids
        tool_ids = triggers.get("tool_ids", [])
        for tool_id in tool_ids:
            if tool_id not in VALID_TOOL_IDS:
                warnings.append(f"Unknown tool_id '{tool_id}' - may not trigger correctly")

        # Validate task_patterns (regex)
        task_patterns = triggers.get("task_patterns", [])
        for pattern in task_patterns:
            try:
                re.compile(pattern)
            except re.error as e:
                errors.append(f"Invalid regex pattern '{pattern}': {e}")

        # Validate priority
        priority = rules.get("priority", 5)
        if not isinstance(priority, int) or priority < 1 or priority > 10:
            warnings.append(f"Priority should be 1-10, got {priority}")

        # Check for empty triggers
        if not tool_ids and not triggers.get("keywords") and not triggers.get("action_names") and not task_patterns:
            warnings.append("No triggers defined - skill will never activate")

        return SkillValidationResult(
            skill_id=skill_id,
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings
        )

    def validate_skill_content(self, skill_id: str, content: str) -> SkillValidationResult:
        """Validate skill content.

        Args:
            skill_id: The skill identifier
            content: The skill markdown content

        Returns:
            SkillValidationResult with validation details
        """
        errors = []
        warnings = []

        # Check content exists
        if not content or not content.strip():
            errors.append("Skill content is empty")
            return SkillValidationResult(skill_id=skill_id, is_valid=False, errors=errors)

        content_len = len(content)

        # Check minimum length (warning only - MIN_SKILL_CONTENT_CHARS behavior unchanged
        # by Task 7; short content still loads, it's just noted).
        if content_len < MIN_SKILL_CONTENT_CHARS:
            warnings.append(f"Skill content very short ({content_len} chars) - may not be useful")

        # Check maximum length against the on-disk ceiling (Task 7: split cap).
        # Short content is already handled above as a warning (not a reject), so the
        # only way this pure helper can still fail here is the "too large" branch.
        length_ok, _length_msg = validate_skill_content_length(content)
        if not length_ok and content_len > MAX_SKILL_FILE_CHARS:
            errors.append(f"Skill content too large ({content_len} chars, max {MAX_SKILL_FILE_CHARS}) - will bloat prompts")

        # Check for markdown structure. Accept an optional YAML frontmatter block
        # (--- ... ---) at the top for interop with the agentskills.io / clawhub
        # open skill standard (P1-1); the heading may follow the frontmatter.
        body = strip_skill_frontmatter(content).strip()
        if not body.startswith('#'):
            warnings.append("Skill should start with a markdown heading")

        # Estimate token count (rough: 4 chars per token)
        estimated_tokens = content_len // 4
        if estimated_tokens > 2000:
            warnings.append(f"Skill uses ~{estimated_tokens} tokens - consider trimming")

        return SkillValidationResult(
            skill_id=skill_id,
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings
        )

    def validate_skill(self, skill_id: str, user_id: Optional[str] = None) -> SkillValidationResult:
        """Validate a complete skill (rules + content).

        Args:
            skill_id: The skill identifier
            user_id: Optional user ID for user skills

        Returns:
            SkillValidationResult with combined validation details
        """
        self._ensure_rules_loaded()

        all_errors = []
        all_warnings = []

        # Get rules
        rules = self.skill_rules.get(skill_id)
        if not rules:
            # Check user rules
            if user_id:
                user_rules, _ = self._load_user_rules(user_id)
                rules = user_rules.get(skill_id)

        if not rules:
            return SkillValidationResult(
                skill_id=skill_id,
                is_valid=False,
                errors=[f"Skill '{skill_id}' not found in rules.json"]
            )

        # Validate rules
        rules_result = self.validate_skill_rules(skill_id, rules)
        all_errors.extend(rules_result.errors)
        all_warnings.extend(rules_result.warnings)

        # Validate content
        content = self._load_skill_content(skill_id, user_id=user_id)
        if not content:
            all_errors.append(f"Skill content file not found for '{skill_id}'")
        else:
            content_result = self.validate_skill_content(skill_id, content)
            all_errors.extend(content_result.errors)
            all_warnings.extend(content_result.warnings)

        return SkillValidationResult(
            skill_id=skill_id,
            is_valid=len(all_errors) == 0,
            errors=all_errors,
            warnings=all_warnings
        )

    def _iter_authored_skill_dirs(self):
        """Yield (name, SKILL.md path) for every authored skill directory on disk.

        "Authored" = a directory directly under ``skills_dir`` with a ``SKILL.md``,
        excluding dotted dirs (``.git`` etc.) and per-user dirs (``user_<id>``,
        which are runtime-created and not part of the shipped library). Shared by
        ``validate_all_authored`` and ``count_authored_skills`` so the two can never
        drift on what counts as "a skill".
        """
        for d in self.skills_dir.iterdir():
            if d.is_dir() and not d.name.startswith((".", "user_")):
                md = d / "SKILL.md"
                if md.exists():
                    yield d.name, md

    def validate_all_authored(self) -> Dict[str, list]:
        """Strict agentskills.io frontmatter compliance across the shipped skill library.

        Runs :func:`skill_validation.validate_authored` (the strict, "would this
        pass the upstream skills-ref reference validator" check) against every
        authored skill's parsed frontmatter. Returns only the skills that have at
        least one issue — a compliant skill is simply absent from the result — so
        an empty dict means the whole library is clean.
        """
        from .skill_validation import validate_authored
        out: Dict[str, list] = {}
        for name, md in self._iter_authored_skill_dirs():
            meta, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
            issues = validate_authored(meta, name)
            if issues:
                out[name] = issues
        return out

    def count_authored_skills(self) -> int:
        """Total number of authored (non-dotted, non-``user_``) skill directories on disk."""
        return sum(1 for _ in self._iter_authored_skill_dirs())

    def provenance_of(self, skill_id: str, user_id: str) -> Optional[str]:
        """Return the recorded ``created_by`` for a skill — LOCAL-ONLY trust source.

        Reads exclusively from ``skill_usage.db``'s ``skill_provenance`` table
        (``SkillUsageStore.get_provenance``, populated at write time by
        ``SkillWriterMixin._record_provenance`` from the ``create_skill``/
        ``patch_skill``/``delete_skill`` **argument**). This is deliberately NEVER
        derived from the skill's own SKILL.md frontmatter: an imported/external
        skill could ship a forged ``metadata: {polyrob-created-by: user}`` block
        to claim trusted ("user") origin and slip past the leaf/background ->
        ``.pending`` quarantine gate (see ``_resolve_pending``/``_NON_USER_AUTHORS``
        in ``skill_writer.py``). ``skill_writer.py`` doesn't even import a
        frontmatter parser, so this can't regress silently — see
        ``tests/unit/agents/task/test_skill_provenance_local.py``.

        Returns ``None`` if unknown / never recorded / anonymous ``user_id``.
        """
        try:
            from modules.skills.skill_usage import get_skill_usage_store
            row = get_skill_usage_store().get_provenance(skill_id, user_id)
        except Exception:
            logger.debug("provenance_of lookup failed for %s/%s", user_id, skill_id, exc_info=True)
            return None
        return row.get("created_by") if row else None


# Module-level singleton for convenience
_skill_manager: Optional[SkillManager] = None


def get_skill_manager() -> SkillManager:
    """Get the singleton SkillManager instance.
    
    Returns:
        SkillManager instance
    """
    global _skill_manager
    if _skill_manager is None:
        _skill_manager = SkillManager()
    return _skill_manager
