"""The system skill index and the gate predicates, written once (081 WS-4.1).

The index maps a skill id to its RULE (``triggers.tool_ids``, ``priority``,
``auto_activate``, ``requires``, ``description``), built from each SKILL.md's
frontmatter: the builtin library and every loaded pack. Tenant skills are merged
over it per session by the skill manager.

The predicates answer "may this session see / load this skill":

- :func:`money_tool_gate_ok` — a money playbook needs its money domain.
- :func:`gate_allows` — the ONE gated + money check that the session catalog and
  ``load_skill`` share.

``triggers`` keeps one key, ``tool_ids``: the domain a money playbook or a gated
skill needs. Keyword and pattern triggers are gone (081 Q2): a skill loads when a
job or goal pins it, or when the model picks its index line.
"""
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set, Tuple

from core.skills import scopes
from core.skills.frontmatter import (
    SkillValidationResult, decode_triggers, parse_bool, parse_frontmatter, validate_skill_id,
)

logger = logging.getLogger(__name__)

# DERIVED from the WS-2 capability table: every registrable tool has a capability
# row, so the table's keys ARE the registrable vocabulary. `tool_manage` is gated
# everywhere and not yet registrable; `worker_manage` (041) is a controller ACTION.
# Registry parity: tests/unit/agents/task/test_valid_tool_ids_parity.py.
from core.tool_capabilities import classified_ids as _classified_ids

VALID_TOOL_IDS = set(_classified_ids()) - {'tool_manage', 'worker_manage'}


# --- the index ------------------------------------------------------------------
# 081 WS-4.2: a skill's SKILL.md frontmatter is its ONLY metadata source. The rule
# is built from the agentskills.io fields and the flat ``metadata.polyrob-*``
# strings; there is no second index file to drift from the body.

#: The frontmatter keys that make a skill a RULE (gate, rank, prerequisites).
_RULE_KEYS = ("polyrob-triggers", "polyrob-priority", "polyrob-auto-activate", "polyrob-requires")


def _int_or(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def rule_from_frontmatter(meta: Optional[dict]) -> Optional[dict]:
    """The rule a SKILL.md declares, or ``None`` when it declares no ``polyrob-*``
    rule key (a plain agentskills.io skill is an index entry with no rule)."""
    meta = meta if isinstance(meta, dict) else {}
    md = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    if not any(k in md for k in _RULE_KEYS):
        return None
    rule: Dict[str, Any] = {
        "triggers": decode_triggers(md.get("polyrob-triggers")),
        "priority": _int_or(md.get("polyrob-priority"), 5),
        "auto_activate": parse_bool(md.get("polyrob-auto-activate", "true")),
        "description": str(meta.get("description") or ""),
    }
    requires = md.get("polyrob-requires")
    if requires:
        ids: Any = requires
        if isinstance(requires, str):
            try:
                ids = json.loads(requires)
            except ValueError:
                ids = requires
        if isinstance(ids, str):  # '"a"' (a JSON string) or 'a, b'
            ids = ids.split(",")
        if not isinstance(ids, (list, tuple)):  # 123 / null / a mapping
            ids = []
        rule["requires"] = [x.strip() for x in ids if isinstance(x, str) and x.strip()]
    if md.get("polyrob-version"):
        rule["version"] = str(md["polyrob-version"])
    return rule


def load_builtin_rules(builtin_root: Path) -> Dict[str, dict]:
    """The rule of every skill directory under ``builtin_root``. Fail-open per
    skill: an unreadable SKILL.md is logged and skipped (never blocks boot)."""
    rules: Dict[str, dict] = {}
    root = Path(builtin_root)
    scope = scopes.SkillScope(name="builtin", root=root, writable=False, trusted=True)
    for sid in scopes.builtin_skill_ids(scope):
        try:
            meta, _ = parse_frontmatter((root / sid / "SKILL.md").read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as e:
            logger.error("skill %s: SKILL.md unreadable (%s) — skipped", sid, e)
            continue
        rule = rule_from_frontmatter(meta)
        if rule is not None:
            rules[sid] = rule
    logger.info("Loaded %d skill rules from %s", len(rules), root)
    return rules


def merge_pack_rules(rules: Dict[str, dict],
                     get_external: Callable[[], Dict[str, "scopes.DiscoveredSkill"]]) -> Set[str]:
    """Add each loaded pack skill's rule (067 P3) under the id it is indexed as
    (``<pack>:<id>`` on a collision). A rule never overrides an existing one.
    Returns the ids added."""
    added: Set[str] = set()
    if not scopes.pack_scopes():
        return added
    for indexed, ds in get_external().items():
        if not ds.scope.startswith("pack:") or indexed in rules:
            continue
        rule = rule_from_frontmatter(ds.meta)
        if rule is None:
            continue  # a plain skill stays a catalog entry
        rules[indexed] = rule
        added.add(indexed)
    return added


def build_index(builtin_root: Path, *, with_packs: bool,
                get_external: Optional[Callable[[], Dict[str, "scopes.DiscoveredSkill"]]] = None
                ) -> Tuple[Dict[str, dict], Set[str]]:
    """``(rules, pack_rule_ids)`` for one builtin root. Packs join only the LIVE
    builtin library (a manager pointed at another root is a test or a sandbox)."""
    rules = load_builtin_rules(builtin_root)
    pack_ids: Set[str] = set()
    if with_packs:
        try:
            pack_ids = merge_pack_rules(rules, get_external or scopes.external_index)
        except Exception:  # pragma: no cover - a pack must never block the rules
            logger.warning("pack skill rules skipped", exc_info=True)
    return rules, pack_ids


def validate_rule(skill_id: str, rules: dict) -> SkillValidationResult:
    """A rule row: a valid id, known tool ids, priority 1-10."""
    errors, warnings = [], []
    errors.extend(validate_skill_id(skill_id)[1])
    triggers = rules.get("triggers", {})
    tool_ids = triggers.get("tool_ids", [])
    for tool_id in tool_ids:
        if tool_id not in VALID_TOOL_IDS:
            warnings.append(f"Unknown tool_id '{tool_id}' - may not trigger correctly")
    priority = rules.get("priority", 5)
    if not isinstance(priority, int) or priority < 1 or priority > 10:
        warnings.append(f"Priority should be 1-10, got {priority}")
    return SkillValidationResult(skill_id=skill_id, is_valid=not errors,
                                 errors=errors, warnings=warnings)


# --- the predicates -------------------------------------------------------------

def money_tool_gate_ok(triggers: Optional[dict], session_tool_ids) -> bool:
    """False when a money playbook's session cannot reach its domain.

    The session must hold a declared tool that is either a MONEY tool
    (``defi_trade``) or a pure read tool of the domain (``defi_data`` — no
    ``high_impact`` capability). A declared ACTION tool that moves no funds —
    ``cronjob`` on ``dca``/``exits`` — does not count (review 2026-09-29 D2).
    Reads ``core.tool_capabilities``, so a new money tool is covered the day it is
    classified. Fail-OPEN: losing a playbook is worse than showing one too often.
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


def gate_allows(rule: Optional[dict], tool_ids, *, listing: bool = False) -> bool:
    """May a session holding ``tool_ids`` see or load the skill of ``rule``?

    * no rule — not a gated skill; other guards apply (True).
    * ``auto_activate: false`` — a GATED skill (P1-1): only when every declared
      ``triggers.tool_ids`` is loaded.
    * otherwise the money gate (067 P0.1). ``listing=True`` with no ``tool_ids``
      is a listing view (console, export, ``/skills``), not a session, and lists
      an auto-activatable money playbook.
    """
    if rule is None:
        return True
    triggers = rule.get("triggers", {}) or {}
    if not rule.get("auto_activate", True):
        gate = set(triggers.get("tool_ids", []) or [])
        return bool(gate and gate.issubset(set(tool_ids or [])))
    if listing and tool_ids is None:
        return True
    return money_tool_gate_ok(triggers, tool_ids or [])
