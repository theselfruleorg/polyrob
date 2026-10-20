"""
Skill Manager - Loads and matches skills for each session.

A session's PINNED skills (a job's or goal's ``payload.skills``, a session
config's ``skills``, the persona seeds) and their ``requires`` load at session
start; every other skill is one line of the short skill index, and the model
loads it with ``load_skill`` (081 Q2).

Where skills live and which one wins is ``core.skills.scopes`` (one ``resolve``);
the system rule index and the gate predicates are ``core.skills.catalog``; the
writer is ``modules.skills.writer``. This module keeps session matching and
prompt delivery.
"""

import logging
import hashlib
from pathlib import Path
from typing import List, Dict, Optional, Set, Any, Tuple
from dataclasses import dataclass

from core.skills import catalog, scopes
from core.skills.catalog import VALID_TOOL_IDS  # noqa: F401 — the vocabulary, imported by callers
from core.skills.frontmatter import (  # noqa: F401 — limits imported by callers
    MAX_SKILL_FILE_CHARS, MAX_SKILL_INJECT_CHARS, MIN_SKILL_CONTENT_CHARS,
    SOFT_INJECT_WARN_CHARS, SkillValidationResult, parse_frontmatter,
    validate_skill_content_length,
)
from core.skills import frontmatter as _frontmatter
from core.skills.frontmatter import when_line as _when_of

logger = logging.getLogger(__name__)


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
    #: 081 WS-4.3: the frontmatter ``metadata.polyrob-when`` line (<= 60 chars) the
    #: index shows; "" when the skill has none (the description is the fallback).
    when: str = ""

    def __getitem__(self, key: str):
        """Dict-style access (``m["id"]``) alongside normal attribute access, so
        catalog consumers can treat every entry uniformly regardless of whether
        it originated from the rule index or from external skill discovery."""
        if key == "id":
            return self.skill_id
        return getattr(self, key)


from modules.skills.writer import SkillWriterMixin


def catalog_line(skill: Any, *, load_first: bool = False) -> str:
    """ONE index entry: ``- id="<id>" — [LOAD FIRST — ]<when>``. The ``when`` line
    (081 WS-4.3, <= 60 chars) says when to use the skill; a skill without one
    shows the first line of its description, cut at 160 characters."""
    desc = (getattr(skill, "when", "") or "").strip()
    if not desc:
        desc = (getattr(skill, "description", "") or skill.skill_id.replace("-", " ")).strip()
        desc = desc.splitlines()[0][:160] if desc else skill.skill_id
    mark = "LOAD FIRST — " if load_first else ""
    return f'- id="{skill.skill_id}" — {mark}{desc}'


class SkillManager(SkillWriterMixin):
    """
    Manages skill loading and matching for sessions.
    
    A session calls :meth:`get_pinned_skills` (its pins) and
    :meth:`get_catalog_skills` (the index) at construction, then
    :meth:`split_progressive` / :meth:`format_progressive` render the skill message.
    """
    
    def __init__(self, skills_dir: Optional[Path] = None):
        """Initialize SkillManager with skills directory.

        Args:
            skills_dir: Path to the BUILTIN (system) skills directory. Defaults
                to the shipped package's ``data/prompts/skills/`` (Task 8:
                ``core.skills.scopes.builtin_scope().root``). Per-tenant
                ``user_<uid>`` reads/writes do NOT use this by default — see
                ``_user_dirs_root()``, which routes them to the writable
                data-home scope instead so they survive a ``polyrob update``
                code-swap. Passing this explicitly (as several existing tests
                do, to isolate BOTH builtin skills AND user writes under
                one tmp root) also redirects user dirs here, preserving that
                pre-existing single-root override contract.
        """
        # Recorded once so `_user_dirs_root()` can detect an override of the
        # TRUE builtin default — whether via this constructor arg or a later
        # direct `.skills_dir =` mutation (both patterns are used by the
        # existing test suite for single-root isolation).
        self._builtin_default_dir = scopes.builtin_scope().root
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
        self._external_index: Optional[Dict[str, scopes.DiscoveredSkill]] = None

        # Task 9: one-time migration of any pre-Task-8 code-tree user_<uid>/
        # skills into data-home. ALWAYS the REAL package tree, never a test's
        # skills_dir override. Fail-open, idempotent, locked, resumable.
        try:
            from modules.skills.migration import migrate_legacy_user_skills
            _moved = migrate_legacy_user_skills(self._builtin_default_dir)
            if _moved:
                logger.info("migrated %d legacy user skill(s) into data-home", _moved)
        except Exception:
            logger.debug("legacy skill migration skipped (non-fatal)", exc_info=True)

        logger.debug(f"SkillManager initialized with skills_dir: {self.skills_dir}")

    def _load_external_skills(self) -> Dict[str, Any]:
        """Pack, project and user-external skills (``core.skills.scopes.external_index``),
        cached once per instance; ``reload_rules()`` forces a re-scan."""
        if self._external_index is None:
            self._external_index = scopes.external_index()
        return self._external_index

    def _ensure_rules_loaded(self) -> None:
        """Build the system rule index once (``core.skills.catalog.build_index``)."""
        if self._rules_loaded:
            return
        self.skill_rules, self._pack_rule_ids = catalog.build_index(
            self.skills_dir,
            with_packs=Path(self.skills_dir) == Path(self._builtin_default_dir),
            get_external=self._load_external_skills)
        # Task 4: fail-open boot warning for strict agentskills.io frontmatter
        # compliance. Observability only — never blocks startup.
        try:
            bad = self.validate_all_authored()
            if bad:
                logger.warning("skill compliance: %d non-compliant skill(s): %s",
                               len(bad), ", ".join(sorted(bad)))
        except Exception:
            logger.debug("skill compliance check skipped", exc_info=True)
        self._rules_loaded = True

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
            ids |= set(scopes.builtin_skill_ids())
        except Exception:
            logger.debug("builtin-id lookup skipped", exc_info=True)
        try:
            if Path(self.skills_dir) != Path(self._builtin_default_dir):
                ids |= set(scopes.builtin_skill_ids(scopes.SkillScope(
                    name="builtin", root=Path(self.skills_dir),
                    writable=False, trusted=True)))
        except Exception:
            logger.debug("custom builtin-id lookup skipped", exc_info=True)
        try:
            for scope in scopes.pack_scopes():
                ids |= set(scopes.builtin_skill_ids(scope))
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
          the session's skill read and the session silently got NO skills.
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

    #: The money-domain gate (``core.skills.catalog.money_tool_gate_ok``).
    _money_tool_gate_ok = staticmethod(catalog.money_tool_gate_ok)

    #: 068 A1: the most prerequisite skills one session may pull in beyond the
    #: pins. Bounded because every injected body costs prompt.
    MAX_PREREQUISITES = 2

    def _with_prerequisites(self, result: List["MatchedSkill"], all_rules: dict,
                            tool_ids, user_id) -> List["MatchedSkill"]:
        """Append the ``requires`` skills of every pinned skill, once each.

        A rule may declare ``polyrob-requires: '["<id>", ...]'`` in its frontmatter. The
        2026-09-25 wrong-token buy ran with the memecoin playbook loaded and the
        identity procedure absent. A prerequisite loads only when its own money-tool
        gate passes for this session, and at most ``MAX_PREREQUISITES`` are added.

        * A prerequisite whose effective rule says ``auto_activate: false`` is NOT
          pulled in: ``requires`` never re-enables a disabled skill. A skill the
          rail PINS explicitly is still loaded — a pin is explicit intent.
        * One pass, no recursion: a prerequisite's own ``requires`` are not expanded.
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
                extra.append(self._entry(req, req_rule, content, user_id,
                                         reason=f"requires:{m.skill_id}",
                                         trigger_type="prerequisite"))
                present.add(req)
        return result + extra

    def _effective_rules(self, user_id: Optional[str]) -> dict:
        """The system rules with the tenant's rules merged over them."""
        self._ensure_rules_loaded()
        all_rules = dict(self.skill_rules)
        if user_id:
            user_rules, _ = self._load_user_rules(user_id)
            self._merge_user_rules(all_rules, user_rules, user_id)
        return all_rules

    def _entry(self, skill_id: str, rule: dict, content: str, user_id: Optional[str], *,
               reason: str, trigger_type: str, priority: Optional[int] = None) -> "MatchedSkill":
        return MatchedSkill(
            skill_id=skill_id,
            priority=rule.get("priority", 5) if priority is None else priority,
            match_reasons=[reason],
            content=content,
            description=self._resolve_skill_description(skill_id, rule, user_id=user_id),
            trigger_type=trigger_type,
            when=self._resolve_skill_when(skill_id, user_id=user_id),
        )

    def get_pinned_skills(
        self,
        seeded_skill_ids: Optional[List[str]] = None,
        tool_ids: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> List[MatchedSkill]:
        """The skills a session loads at START: its PINS, then their prerequisites.

        081 Q2 (owner, 2026-10-15): only a pinned skill loads at session start — a
        cron job's or goal's ``payload.skills``, ``TaskSessionConfig.skills`` and
        the persona seeds. Every other session (owner chat, room, correspondent, a
        goal or cron run with no pin) gets the short skill index and chooses with
        ``load_skill``. There is no keyword matcher: it auto-loaded 705 skills for
        42 explicit loads and pinned the user guide on any "why" or "help".

        An unknown id or an id with no SKILL.md is skipped (the caller names a
        missing rail pin). A pin is explicit intent, so it loads even when its
        rule is gated.
        """
        if not seeded_skill_ids:
            return []
        all_rules = self._effective_rules(user_id)
        result: List[MatchedSkill] = []
        for sid in dict.fromkeys(seeded_skill_ids):
            rule = all_rules.get(sid)
            if not rule:
                continue  # unknown id → skip (fail-open)
            content = self._load_skill_content(sid, user_id=user_id)
            if not content:
                continue  # no SKILL.md → skip (fail-open)
            result.append(self._entry(sid, rule, content, user_id, reason="seeded:preset",
                                      trigger_type="seeded", priority=rule.get("priority", 99)))
        result = self._with_prerequisites(result, all_rules, tool_ids or [], user_id)
        if result:
            logger.info(f"Pinned {len(result)} skills for session: {[m.skill_id for m in result]}")
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

        This exposes every available skill so the agent can DISCOVER and ``load_skill``
        any of them on demand — fixing the gap where a session that matched zero
        triggers got an empty catalog and could load nothing. Bodies are preloaded so
        ``load_skill`` serves without a disk re-read. Sorted by priority, capped.

        P1-1: a GATED skill (``auto_activate: false`` — e.g. the money/trading
        playbooks) is surfaced ONLY when the session has loaded its required tools
        (``triggers.tool_ids`` all present in *tool_ids*) — that is what those triggers
        were written for. Otherwise it stays hidden AND the load_skill fallback refuses
        it (see :meth:`may_load_skill`), so the gate is real, not advisory.

        067 P0.1: in a SESSION context (*tool_ids* given) an auto-activatable money
        playbook (e.g. ``treasury-trading``) passes the money-tool gate
        (:meth:`_money_tool_gate_ok`). ``tool_ids=None`` is a listing
        view (console, export, ``/skills``), not a session, and lists it.
        """
        all_rules = self._effective_rules(user_id)

        entries = []
        # 067 P4: ids a gate below withheld. A pack skill (e.g. the markets pack's
        # polymarket-trading) is ALSO discovered as an external skill, so the append
        # further down must not put a withheld one back.
        withheld = set()
        for skill_id, rules in all_rules.items():
            # P1-1 gated + 067 P0.1 money gate: the ONE predicate load_skill uses.
            if not catalog.gate_allows(rules, tool_ids, listing=True):
                withheld.add(skill_id)
                continue
            content = self._load_skill_content(skill_id, user_id=user_id)
            if not content:
                continue
            entries.append(self._entry(skill_id, rules, content, user_id,
                                       reason="catalog", trigger_type="catalog"))

        # Task 14: append externally-discovered (agentskills.io ecosystem) skills that
        # aren't already covered by a builtin/user rule of the same id.
        # P1-7: external skills (from ~/.agents/skills, ~/.claude/skills) are untrusted
        # third-party content that gets pinned into EVERY session's catalog prompt —
        # scan the description AND body, and skip any that trips the injection scan
        # (fail-OPEN if the scanner is unavailable, fail-CLOSED if it raises), matching
        # the writer's P3-1 stance that a catalog description is an injection vector.
        existing_ids = {m.skill_id for m in entries}
        for ext_id, ds in self._load_external_skills().items():
            if ext_id in existing_ids or ext_id in withheld:
                continue
            # The catalog lists only what load_skill serves: an id that a
            # tenant skill shares with a project/user-external one is refused.
            served = self._resolve(ext_id, user_id)
            if served is None or served.external is not ds:
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
            entries.append(MatchedSkill(
                skill_id=ext_id,
                priority=5 if ds.scope.startswith("pack:") else self.EXTERNAL_SKILL_PRIORITY,
                match_reasons=["catalog"],
                content=ds.body,
                description=ext_desc,
                trigger_type="catalog",
                source=ds.scope,
                when=_when_of(ds.meta),
            ))
            existing_ids.add(ext_id)

        entries.sort(key=lambda m: (m.priority, m.skill_id))
        return entries[:max_skills]

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
        (``core.skills.scopes.skills_data_home()``) so a create/patch/delete survives
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
        return scopes.skills_data_home()

    def _resolve(self, skill_id: str, user_id: Optional[str] = None):
        """``core.skills.scopes.resolve`` over THIS manager's roots (a test or a
        sandbox may redirect ``skills_dir``, and with it the tenant root)."""
        uid = self._require_user(user_id) if user_id else None
        return scopes.resolve(skill_id, uid, builtin_root=Path(self.skills_dir),
                              users_root=self._user_dirs_root(),
                              external=self._load_external_skills())

    def resolve_skill_dir(self, skill_id: str, user_id: Optional[str] = None) -> Optional[Path]:
        """A skill's on-disk directory for read-only resource access (Task 17), by
        the ONE precedence order (:meth:`_resolve`). ``None`` when it is not on
        disk or the id could escape the directory join. A tenant directory must
        also pass the confined read (no symlink out of the tenant root)."""
        try:
            res = self._resolve(skill_id, user_id)
        except Exception:
            logger.debug("resolve_skill_dir: lookup failed for %s", skill_id, exc_info=True)
            return None
        if res is None:
            return None
        if res.scope == "user":
            try:
                self._read_skill_text(res.path / "SKILL.md")
            except (OSError, ValueError):
                return None
        return res.path

    def _load_user_rules(self, user_id: str) -> tuple:
        """``(rules, user_dir)`` — the tolerant view over the ONE parser
        (:meth:`_load_user_rules_raw`): an unreadable or non-object file is
        logged and loads as no rules, so one bad file never stops a session."""
        uid = self._require_user(user_id)
        if uid is None:
            return {}, None
        user_dir = self._user_root(uid)
        try:
            rules = self._load_user_rules_raw(uid)
        except Exception as e:
            logger.warning("Failed to load user rules for %s: %s", user_id, e)
            return {}, user_dir
        if not isinstance(rules, dict):
            logger.warning("user skill rules for %s are not an object — ignored", user_id)
            return {}, user_dir
        return rules, user_dir

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
        """Gate for on-demand loading (P1-1, 067 P0.1): the same predicate the
        session catalog applies (``core.skills.catalog.gate_allows``), so listing
        and loading agree. An unknown id is loadable; other guards apply."""
        return catalog.gate_allows(self.get_skill_rule(skill_id, user_id=user_id), tool_ids)

    def _load_skill_content(self, skill_id: str, user_id: Optional[str] = None) -> str:
        """The skill's body, frontmatter stripped, by the ONE precedence order
        (``core.skills.scopes.resolve``: builtin > user > pack > project >
        user-external). ``""`` when nothing serves the id.

        The frontmatter is metadata for tooling, not instructions for the LLM: it is
        parsed off here, the single source point, and cached separately
        (:meth:`_get_skill_meta`). A tenant skill with a recorded revision is
        hash-verified on EVERY load (another worker may have changed it), and is
        refused when its revision metadata cannot be read.
        """
        if (not skill_id or "/" in skill_id or "\\" in skill_id or ".." in skill_id
                or skill_id != skill_id.strip()):
            return ""
        if user_id is not None and self._require_user(user_id) is None:
            return ""
        cache_key = f"{user_id}:{skill_id}" if user_id else skill_id
        res = self._resolve(skill_id, user_id)
        if res is None:
            if cache_key in self.skill_cache:  # an in-memory (test-injected) skill
                return self.skill_cache[cache_key]
            logger.warning(f"Skill file not found: {self.skills_dir / skill_id / 'SKILL.md'}")
            return ""
        if res.external is not None:
            meta, body = res.external.meta, res.external.body
        else:
            expected = None
            if res.scope == "user":
                try:
                    expected = self._expected_content_hash(user_id, skill_id)
                except Exception:
                    logger.warning("refusing user skill %s: could not read revision metadata", skill_id)
                    return ""
            if cache_key in self.skill_cache and expected is None:
                return self.skill_cache[cache_key]
            try:
                skill_file = res.path / "SKILL.md"
                raw = (self._read_skill_text(skill_file) if res.scope == "user"
                       else skill_file.read_text(encoding='utf-8'))
            except Exception as e:
                logger.error(f"Failed to load skill content for '{skill_id}': {e}")
                return ""
            if expected is not None and hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected:
                logger.warning("refusing mismatched user skill body for '%s'", skill_id)
                return ""
            meta, body = parse_frontmatter(raw)
        self.skill_meta_cache[cache_key] = meta
        body = self._note_allowed_tools(cache_key, skill_id, meta, body)
        self.skill_cache[cache_key] = body
        logger.debug(f"Loaded {res.scope} skill content for '{skill_id}' ({len(body)} chars)")
        return body

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

        The SKILL.md frontmatter ``description`` is the source of truth; the
        rule's ``description`` is the fallback (a tenant skill whose body has no
        frontmatter keeps the one its writer recorded). Callers MUST call
        ``_load_skill_content(skill_id, user_id=user_id)`` first so the
        frontmatter cache is populated.
        """
        meta = self._get_skill_meta(skill_id, user_id=user_id)
        return meta.get("description") or rules.get("description", "")

    def _resolve_skill_when(self, skill_id: str, user_id: Optional[str] = None) -> str:
        """The index line (``metadata.polyrob-when``) from the cached frontmatter."""
        return _when_of(self._get_skill_meta(skill_id, user_id=user_id))
    
    def format_skills_for_prompt(self, matched_skills: List[MatchedSkill]) -> str:
        """The pinned skill bodies, each in a ``<skill id=...>`` tag, in one ``<skills>`` block."""
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
                    "injecting in full anyway; consider trimming the skill",
                    skill.skill_id, len(skill.content), SOFT_INJECT_WARN_CHARS,
                )
            sections.append(f'<skill id="{skill.skill_id}">')
            sections.append(skill.content)
            sections.append("</skill>")
            sections.append("")

        sections.append("</skills>")
        return "\n".join(sections)
    
    def format_skill_catalog(self, matched_skills: List[MatchedSkill],
                             load_first: Optional[Set[str]] = None) -> str:
        """The short skill index (081 Q2): one line per skill, id + when to use it.

        The model loads a body with ``load_skill``. ``load_first`` (D5): ids this
        run PINNED that did not fit the eager budget; they are listed first with a
        ``LOAD FIRST`` mark. Returns "" when there is no skill.
        """
        if not matched_skills:
            return ""
        load_first = set(load_first or ())

        lines = [
            "<skill-catalog>",
            'Before you act, call load_skill(skill_id="<id>") for the skill whose line fits the task.',
        ]
        if load_first:
            lines.append("Entries marked LOAD FIRST are pinned to this run: load each one "
                         "before you act.")
        lines.append("")
        ordered = ([s for s in matched_skills if s.skill_id in load_first]
                   + [s for s in matched_skills if s.skill_id not in load_first])
        for skill in ordered:
            lines.append(catalog_line(skill, load_first=skill.skill_id in load_first))
        lines.append("</skill-catalog>")
        return "\n".join(lines)

    #: D5: the most body characters eager-injected for the skills the session
    #: PINNED (prerequisites first). ~5k tokens.
    #: A skill that does not fit is marked LOAD FIRST in the catalog instead.
    EAGER_INJECT_BUDGET_CHARS = MAX_SKILL_INJECT_CHARS

    #: pinning order under the budget: safety prerequisites, then rail/persona seeds.
    _EAGER_ORDER = {"prerequisite": 0, "seeded": 1}

    def split_progressive(self, session_matched: List[MatchedSkill],
                          budget: Optional[int] = None
                          ) -> Tuple[List[MatchedSkill], Set[str]]:
        """Split the session's PINNED skills into (eager, load_first) — D5.

        Every pinned body that fits the budget is in the skill message in full,
        prerequisites first; the rest are index entries marked LOAD FIRST.
        Bounded: at most ``budget`` characters of bodies.
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
        """``core.skills.frontmatter.validate_skill_id``."""
        return _frontmatter.validate_skill_id(skill_id)

    def validate_skill_rules(self, skill_id: str, rules: dict) -> SkillValidationResult:
        """``core.skills.catalog.validate_rule``."""
        return catalog.validate_rule(skill_id, rules)

    def validate_skill_content(self, skill_id: str, content: str) -> SkillValidationResult:
        """``core.skills.frontmatter.validate_skill_content``."""
        return _frontmatter.validate_skill_content(skill_id, content)

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
                errors=[f"Skill '{skill_id}' has no rule"]
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

        Runs :func:`core.skills.frontmatter.validate_authored` (the strict, "would this
        pass the upstream skills-ref reference validator" check) against every
        authored skill's parsed frontmatter. Returns only the skills that have at
        least one issue — a compliant skill is simply absent from the result — so
        an empty dict means the whole library is clean.
        """
        from core.skills.frontmatter import validate_authored
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
        in ``modules/skills/writer.py``). The writer doesn't even import a
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
