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
from core.tool_capabilities import TOOL_CAPABILITIES as _TOOL_CAPABILITIES

VALID_TOOL_IDS = set(_TOOL_CAPABILITIES) - {'tool_manage'}


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

    def _prune_bodiless_rules(self) -> None:
        """Drop any auto_activate system rule whose SKILL.md body is missing."""
        orphans = [
            sid for sid, rules in self.skill_rules.items()
            if rules.get("auto_activate", True)
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
        task_lower = task.lower()
        
        # Combine system + user rules
        all_rules = dict(self.skill_rules)
        user_skills_dir = None
        
        if user_id:
            user_rules, user_skills_dir = self._load_user_rules(user_id)
            all_rules.update(user_rules)
        
        matches = []
        
        for skill_id, rules in all_rules.items():
            if not rules.get("auto_activate", True):
                continue

            triggers = rules.get("triggers", {})

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
        """
        self._ensure_rules_loaded()
        all_rules = dict(self.skill_rules)
        if user_id:
            user_rules, _ = self._load_user_rules(user_id)
            all_rules.update(user_rules)

        session_tool_ids = set(tool_ids or [])
        catalog = []
        for skill_id, rules in all_rules.items():
            if not rules.get("auto_activate", True):
                # P1-1: surface a gated skill only when its required tools are loaded.
                gate_tool_ids = set(rules.get("triggers", {}).get("tool_ids", []))
                if not (gate_tool_ids and gate_tool_ids.issubset(session_tool_ids)):
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
            if ext_id in existing_ids:
                continue
            ext_desc = ds.meta.get("description", "")
            if self._external_content_suspicious(ext_id, ext_desc, ds.body):
                continue
            catalog.append(MatchedSkill(
                skill_id=ext_id,
                priority=5,
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
    def _external_content_suspicious(skill_id: str, description: str, body: str) -> bool:
        """True if an external skill's description/body trips the injection scan (P1-7).

        Fail-OPEN when the scanner can't be imported (parity with the rest of the
        codebase); fail-CLOSED (treat as suspicious) when the scanner itself raises.
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
                user_dir = self._user_dirs_root() / f"user_{user_id}" / skill_id
                if user_dir.exists():
                    return user_dir
            except OSError:
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
        user_dir = self._user_dirs_root() / f"user_{user_id}"
        rules_file = user_dir / "rules.json"
        
        if rules_file.exists():
            try:
                with open(rules_file) as f:
                    rules = json.load(f)
                logger.debug(f"Loaded {len(rules)} user skill rules for {user_id}")
                return rules, user_dir
            except Exception as e:
                logger.warning(f"Failed to load user rules for {user_id}: {e}")
        
        return {}, user_dir

    def get_skill_rule(self, skill_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        """Return the merged rule dict for a skill_id (user rule shadows builtin), or None."""
        self._ensure_rules_loaded()
        if user_id:
            user_rules, _ = self._load_user_rules(user_id)
            if skill_id in user_rules:
                return user_rules[skill_id]
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
        """
        rule = self.get_skill_rule(skill_id, user_id=user_id)
        if rule is None:
            return True  # unknown to rules.json — not a gated skill; other guards apply
        if rule.get("auto_activate", True):
            return True
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
        # Create cache key that includes user context
        cache_key = f"{user_id}:{skill_id}" if user_id else skill_id

        # Check cache first
        if cache_key in self.skill_cache:
            return self.skill_cache[cache_key]

        # Check user skills first if user_id provided
        if user_id:
            user_skill_file = self._user_dirs_root() / f"user_{user_id}" / skill_id / "SKILL.md"
            if user_skill_file.exists():
                try:
                    raw = user_skill_file.read_text(encoding='utf-8')
                    meta, body = parse_frontmatter(raw)
                    self.skill_meta_cache[cache_key] = meta
                    self.skill_cache[cache_key] = body
                    logger.debug(f"Loaded user skill content for '{skill_id}' ({len(body)} chars)")
                    return body
                except Exception as e:
                    logger.error(f"Failed to load user skill content for '{skill_id}': {e}")

        # Try to load from system skill directory
        skill_dir = self.skills_dir / skill_id
        skill_file = skill_dir / "SKILL.md"

        if skill_file.exists():
            try:
                raw = skill_file.read_text(encoding='utf-8')
                meta, body = parse_frontmatter(raw)
                self.skill_meta_cache[cache_key] = meta
                self.skill_cache[cache_key] = body
                logger.debug(f"Loaded skill content for '{skill_id}' ({len(body)} chars)")
                return body
            except Exception as e:
                logger.error(f"Failed to load skill content for '{skill_id}': {e}")
        else:
            logger.warning(f"Skill file not found: {skill_file}")

        # Task 14: external (~/.agents/skills, ~/.claude/skills, ...) lookup — LAST,
        # so builtin/user skills always take precedence over ecosystem ones.
        ext = self._load_external_skills().get(skill_id)
        if ext is not None:
            self.skill_meta_cache[cache_key] = ext.meta
            self.skill_cache[cache_key] = ext.body
            logger.debug(f"Loaded external ({ext.scope}) skill content for '{skill_id}' ({len(ext.body)} chars)")
            return ext.body

        return ""

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
    
    def format_skill_catalog(self, matched_skills: List[MatchedSkill]) -> str:
        """Format matched skills as a compact catalog (S-1 progressive disclosure).

        Lists only id + one-line description per skill (~20-30 tok each) instead of
        the full bodies. The agent loads a skill's full instructions on demand via
        the load_skill(skill_id) tool. Returns "" when nothing matched.
        """
        if not matched_skills:
            return ""

        lines = [
            "<skill-catalog>",
            f"{len(matched_skills)} skill(s) are available for this task. Each is a "
            "detailed workflow you should follow when relevant.",
            "Call load_skill(skill_id=\"<id>\") to load a skill's FULL instructions "
            "BEFORE doing the work it covers. Load only what the current step needs.",
            "",
        ]
        for skill in matched_skills:
            desc = (skill.description or skill.skill_id.replace("-", " ")).strip()
            # Keep each line short — one sentence of description at most.
            desc = desc.splitlines()[0][:160] if desc else skill.skill_id
            lines.append(f'- id="{skill.skill_id}" — {desc}')
        lines.append("</skill-catalog>")
        return "\n".join(lines)

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
