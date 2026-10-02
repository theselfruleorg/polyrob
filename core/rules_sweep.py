"""060 WS-4 — sweep a rule across every instruction surface (2026-09-23).

"Which standing instructions contradict this rule?" used to be a grep an
operator ran by hand after the fact (the 09-21 X post: an owner rule said no,
a cron job's prose said yes, and the prose was the turn). This is that question
as a function:

- It reads the SIX surfaces (``core.instruction_surfaces``): owner rules (the
  ACTIVE part only), SOUL, skills (injected by the tool tier), rail prose (cron
  jobs + live goals), and workspace docs marked ``kind: instruction``. The system
  prompt is code and is named as not swept.
- ⚠️ A RECORD is never swept (060 WS-3): a dated log is history, and proposing
  an edit to it is proposing to falsify it.
- A line CONFLICTS when exactly one of (rule, line) prohibits and they share at
  least two content words — the same deterministic, LLM-free test
  ``self_evolution.detect_conflicts`` applies to a pending draft.
- ADVISORY only: it reports; it never edits anything. Precedence decides the
  outcome at run time (a later owner rule outranks rail prose — owner decision
  Q1, 2026-09-23); the sweep is how the owner SEES what the rule overrides.

Pure core: stdlib + other ``core`` modules; skill texts are passed in.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

#: A conflict needs this many shared content words (same bar as detect_conflicts).
MIN_OVERLAP = 2
#: The most hits one sweep returns (the rest are counted).
MAX_HITS = 12
#: Live goals and active objectives can both supply future run instructions.
from core.goal_vocab import LIVE_STATUS_ORDER, OBJ_ACTIVE
_LIVE_GOAL_STATUSES = (*LIVE_STATUS_ORDER, OBJ_ACTIVE)


@dataclass(frozen=True)
class SweepHit:
    surface: str
    where: str
    line: str
    shared: Tuple[str, ...]

    def render(self) -> str:
        return (f"[{self.surface}] {self.where}: \"{self.line[:160]}\" "
                f"(shares: {', '.join(self.shared[:4])})")


def _prohibits(text: str) -> bool:
    from core.self_evolution import _PROHIBITION_RE
    return bool(_PROHIBITION_RE.search(text or ""))


def _terms(text: str) -> set:
    from core.self_evolution import _significant_terms
    return _significant_terms(text or "")


def _scan(rule: str, surface: str, where: str, text: str) -> List[SweepHit]:
    """Each rule LINE against each surface line (a multi-line rule is several rules)."""
    rules = [(_prohibits(r), _terms(r)) for r in (rule or "").splitlines() if r.strip()]
    hits: List[SweepHit] = []
    seen = set()
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("#-* ").strip()
        if not line or line.startswith("---") or line in seen:
            continue
        ban = _prohibits(line)
        terms = _terms(line)
        for rule_ban, rule_terms in rules:
            if ban == rule_ban:
                continue  # both forbid, or both allow: they agree
            shared = rule_terms & terms
            if len(shared) >= MIN_OVERLAP:
                seen.add(line)
                hits.append(SweepHit(surface, where, line, tuple(sorted(shared))))
                break
    return hits


def _rows(db: str, sql: str, params: tuple) -> List[dict]:
    if not db or not os.path.exists(db):
        return []
    from core.sqlite_util import execute_retry
    return [dict(r) for r in (execute_retry(db, sql, params, fetch="all") or [])]


def _rail_texts(uid: str, cron_db: str, goals_db: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    texts: List[Tuple[str, str]] = []
    notes: List[str] = []
    try:
        for r in _rows(cron_db, "SELECT id, task FROM cron_jobs WHERE enabled=1 AND user_id=?", (uid,)):
            texts.append((f"cron {str(r.get('id'))[:8]}", r.get("task") or ""))
    except Exception as e:
        notes.append(f"cron store unreadable ({type(e).__name__})")
    try:
        marks = ",".join("?" for _ in _LIVE_GOAL_STATUSES)
        for r in _rows(goals_db, f"SELECT id, title, body, payload FROM goals WHERE user_id=? "
                                 f"AND status IN ({marks})", (uid, *_LIVE_GOAL_STATUSES)):
            try:
                payload = json.loads(r.get("payload") or "{}")
            except ValueError:
                payload = {}
            task = payload.get("task") if isinstance(payload, dict) else ""
            parts = [r.get("title"), r.get("body"), task]
            # 036 rails keep their future task prose in recurrence legs, before
            # any dispatchable child goal exists. The owner may govern it now.
            recurrence = payload.get("recurrence") if isinstance(payload, dict) else None
            if isinstance(recurrence, dict):
                for leg in recurrence.get("legs") or []:
                    if isinstance(leg, dict):
                        parts.extend((leg.get("title"), leg.get("body")))
            body = "\n".join(p for p in parts if isinstance(p, str) and p)
            texts.append((f"goal {str(r.get('id'))[:8]}", body))
    except Exception as e:
        notes.append(f"goal board unreadable ({type(e).__name__})")
    return texts, notes


def sweep(rule: str, *, user_id: str, data_dir: str, instance_id: str,
          cron_db: str, goals_db: str,
          skills: Optional[Iterable[Tuple[str, str]]] = None) -> dict:
    """Every standing instruction that appears to contradict ``rule``.

    Returns ``{"hits": [SweepHit], "more": n, "swept": [surface names],
    "notes": [str]}``. Never raises for an unreadable surface — it is named
    in ``notes`` instead (an unswept surface must not read as a clean one).
    """
    from core.doc_kind import KIND_INSTRUCTION, classify_tree, doc_kind
    from core.instance import load_owner_doc, load_self_context
    rule = (rule or "").strip()
    out = {"hits": [], "more": 0, "swept": [], "notes": [
        "system prompt: code — not swept (a deploy changes it)"]}
    if not rule:
        out["notes"].append("empty rule — nothing to sweep")
        return out
    hits: List[SweepHit] = []
    uid = str(user_id or "")

    hits += _scan(rule, "owner_rules", "owner.md", load_owner_doc(data_dir, uid, instance_id))
    out["swept"].append("owner_rules")
    hits += _scan(rule, "soul", "identity", load_self_context(data_dir))
    out["swept"].append("soul")
    if skills is None:
        out["notes"].append("skills: not provided by the caller — not swept")
    else:
        for sid, body in skills:
            hits += _scan(rule, "skills", f"skill {sid}", body)
        out["swept"].append("skills")
    rails, notes = _rail_texts(uid, cron_db, goals_db)
    out["notes"] += notes
    for where, text in rails:
        hits += _scan(rule, "rail_prose", where, text)
    out["swept"].append("rail_prose")
    ws = str(Path(data_dir) / "workspace")
    tree = classify_tree(ws)
    if tree.get("state"):
        out["notes"].append(f"workspace: {tree['state']}")
    else:
        for rel in tree.get("instruction_docs") or []:
            try:
                text = Path(ws, rel).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                out["notes"].append(f"workspace {rel}: unreadable ({type(e).__name__})")
                continue
            if doc_kind(text) == KIND_INSTRUCTION:
                hits += _scan(rule, "workspace_doctrine", rel, text)
        out["notes"].append(
            f"workspace: {tree.get('record', 0) + tree.get('undeclared', 0)} record(s) "
            "not swept (history is never edited)")
        out["swept"].append("workspace_doctrine")
    out["hits"] = hits[:MAX_HITS]
    out["more"] = max(0, len(hits) - MAX_HITS)
    return out


def render_sweep(result: dict, rule: str) -> str:
    """One owner-readable block for the sweep result."""
    from core.instruction_surfaces import PRECEDENCE_RULE
    hits = result.get("hits") or []
    head = (f"Sweep for: \"{rule[:120]}\" — "
            + (f"{len(hits) + result.get('more', 0)} line(s) may contradict it:" if hits
               else "nothing in the standing instructions contradicts it."))
    lines = [head] + [f"- {h.render()}" for h in hits]
    if result.get("more"):
        lines.append(f"- … +{result['more']} more")
    lines.append("Swept: " + ", ".join(result.get("swept") or []) + ".")
    lines += [f"Note: {n}" for n in result.get("notes") or []]
    if hits:
        lines.append("Advisory only — nothing was changed. " + PRECEDENCE_RULE)
    return "\n".join(lines)


def rail_instruction_lines(task: str, payload: Optional[dict] = None) -> List[str]:
    """How an owner seat shows a job's prose: as the SCOPED instruction it is."""
    lines = ["instruction (the rail's own — a later owner rule outranks it):"]
    lines += [f"  {l}" for l in (task or "").strip().splitlines()] or ["  (empty)"]
    skills = (payload or {}).get("skills") if isinstance(payload, dict) else None
    if skills:
        lines.append("pinned skills: " + ", ".join(str(s) for s in skills))
    return lines


__all__ = ["SweepHit", "sweep", "render_sweep", "rail_instruction_lines", "MIN_OVERLAP"]
