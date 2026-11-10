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
  least two content words — a deterministic, LLM-free test.
- ADVISORY only: it reports; it never edits anything. Precedence decides the
  outcome at run time (a later owner rule outranks rail prose — owner decision
  Q1, 2026-09-23); the sweep is how the owner SEES what the rule overrides.
- :func:`conflict_check` is the same test run the other way: EVERY active owner
  rule against enabled job text, live goal text and pinned skills, with both quotes.

Pure core: stdlib + other ``core`` modules; skill texts are passed in.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

#: A conflict needs this many shared content words.
MIN_OVERLAP = 2
#: The most hits one sweep returns (the rest are counted).
MAX_HITS = 12
#: Live goals can supply future run instructions.
from core.goal_vocab import LIVE_STATUS_ORDER
_LIVE_GOAL_STATUSES = LIVE_STATUS_ORDER


@dataclass(frozen=True)
class SweepHit:
    surface: str
    where: str
    line: str
    shared: Tuple[str, ...]
    rule: str = ""

    def render(self) -> str:
        return (f"[{self.surface}] {self.where}: \"{self.line[:160]}\" "
                f"(shares: {', '.join(self.shared[:4])})")


_PROHIBITION_RE = re.compile(
    r"\b(no|not|never|stop|cease|halt|don'?t|do not|avoid|forbid(?:den)?|"
    r"disallow(?:ed)?|prohibit(?:ed)?)\b", re.IGNORECASE)

_STOPWORDS = frozenset({
    "the", "and", "for", "all", "any", "with", "from", "this", "that", "into",
    "only", "your", "will", "must", "should", "until", "when", "what", "content",
    "rule", "owner", "directive", "priority", "highest", "effective",
    "immediately", "overrides", "policy", "posting", "posts", "post", "report",
    "reporting", "never", "stop", "always",
})


def _prohibits(text: str) -> bool:
    return bool(_PROHIBITION_RE.search(text or ""))


def _terms(text: str) -> set:
    """Content words, >=3 chars, minus stopwords; hyphens split."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS}


def _scan(rule: str, surface: str, where: str, text: str) -> List[SweepHit]:
    """Each rule LINE against each surface line (a multi-line rule is several rules)."""
    rules = [(r.strip(), _prohibits(r), _terms(r)) for r in (rule or "").splitlines() if r.strip()]
    hits: List[SweepHit] = []
    seen = set()
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("#-* ").strip()
        if not line or line.startswith("---") or line in seen:
            continue
        ban = _prohibits(line)
        terms = _terms(line)
        for rule_line, rule_ban, rule_terms in rules:
            if ban == rule_ban:
                continue  # both forbid, or both allow: they agree
            shared = rule_terms & terms
            if len(shared) >= MIN_OVERLAP:
                seen.add(line)
                hits.append(SweepHit(surface, where, line, tuple(sorted(shared)), rule_line))
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


def same_subject_lines(new_text: str, skill_id: str, rules_text: str) -> List[str]:
    """The owner-rule lines a programme-term change in ``skill_id`` supersedes
    (095 W10, R8). Pure. No new matcher: the sweep's own conflict test
    (:func:`_scan`) plus any rule line that names the skill id."""
    hits = [h.line for h in _scan(new_text, "owner_rules", "owner.md", rules_text)]
    sid = (skill_id or "").strip().lower()
    if sid:
        for raw in (rules_text or "").splitlines():
            line = raw.strip().lstrip("#-* ").strip()
            if line and not raw.lstrip().startswith("#") and sid in line.lower():
                hits.append(line)
    return list(dict.fromkeys(hits))


def pinned_by_a_rail(skill_id: str, user_id: str, cron_db: str, goals_db: str) -> bool:
    """Whether an enabled cron job or a live goal of this tenant pins ``skill_id``
    (``payload.skills``) — i.e. the skill holds a lane's programme terms."""
    return skill_id in pinned_skill_ids(user_id, cron_db, goals_db)


def pinned_skill_ids(user_id: str, cron_db: str, goals_db: str) -> List[str]:
    """Every skill an enabled cron job or a live goal of this tenant pins, in order."""
    from core.config_policy.rigs import pinned_skills
    out: List[str] = []
    marks = ",".join("?" for _ in _LIVE_GOAL_STATUSES)
    queries = ((cron_db, "SELECT payload FROM cron_jobs WHERE enabled=1 AND user_id=?",
                (str(user_id),)),
               (goals_db, f"SELECT payload FROM goals WHERE user_id=? AND status IN ({marks})",
                (str(user_id), *_LIVE_GOAL_STATUSES)))
    for db, sql, params in queries:
        try:
            rows = _rows(db, sql, params)
        except Exception:
            continue
        for r in rows:
            try:
                payload = json.loads(r.get("payload") or "{}")
            except ValueError:
                continue
            if isinstance(payload, dict):
                out += [sid for sid in pinned_skills(payload) if sid not in out]
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


#: The most candidates one conflict check returns (the rest are counted).
CHECK_MAX_HITS = 40
#: A state word says nothing about WHICH job a line is about.
_STATE_TERMS = frozenset({"job", "jobs", "cron", "cronjob", "lane", "off", "hold", "enabled",
                          "disabled", "paused", "stopped", "cancelled", "canceled"})


def _subject_terms(text: str) -> set:
    """Content words with a plural ``s`` dropped, state words left out."""
    return {w[:-1] if w.endswith("s") else w for w in _terms(text)} - _STATE_TERMS


def _state_line_pairs(rule_lines: List[str], rails: List[Tuple[str, str]]) -> List[dict]:
    """Each active line that holds a job's state, with the job lines on its subject.
    The prohibition test cannot see "the buyback is OFF": it has no prohibition word."""
    from core.doc_claims import job_state_claim
    out: List[dict] = []
    for rule in rule_lines:
        if not job_state_claim(rule):
            continue
        subject = _subject_terms(rule)
        quotes = []
        for where, text in rails:
            line = next((l.strip() for l in (text or "").splitlines()
                         if subject & _subject_terms(l)), "")
            if line:
                quotes.append((where, line))
        out.append({"rule": rule, "quotes": quotes})
    return out


def conflict_check(*, user_id: str, data_dir: str, instance_id: str, cron_db: str,
                   goals_db: str, skills: Optional[Iterable[Tuple[str, str]]] = None) -> dict:
    """Every active owner rule against enabled job text, live goal text and pinned
    skills. Deterministic and read-only; a hit is a CANDIDATE that shares words, not a
    judged conflict. ``{"hits", "more", "state_lines", "counts", "notes"}``. An unreadable
    source is named in ``notes``: "could not read" never renders as "no conflict"."""
    from core.doc_claims import active_rule_lines, strip_stamp
    from core.instance import is_unreadable_note, load_owner_doc
    uid = str(user_id or "")
    out = {"hits": [], "more": 0, "state_lines": [], "notes": [],
           "counts": {"rules": 0, "rails": 0, "skills": 0}}
    doc = load_owner_doc(data_dir, uid, instance_id)
    if doc.startswith("[BLOCKED") or is_unreadable_note(doc):
        out["notes"].append("owner rules: could not read them, so nothing was checked")
        return out
    rule_lines = [strip_stamp(l).strip() for l in active_rule_lines(doc)]
    rails, notes = _rail_texts(uid, cron_db, goals_db)
    out["notes"] += notes
    bodies = list(skills or [])
    if skills is None:
        out["notes"].append("skills: not provided by the caller — not checked")
    out["counts"] = {"rules": len(rule_lines), "rails": len(rails), "skills": len(bodies)}
    rule = "\n".join(rule_lines)
    hits: List[SweepHit] = []
    for where, text in rails:
        hits += _scan(rule, "rail_prose", where, text)
    for sid, body in bodies:
        hits += _scan(rule, "skills", f"skill {sid}", body)
    out["hits"] = hits[:CHECK_MAX_HITS]
    out["more"] = max(0, len(hits) - CHECK_MAX_HITS)
    out["state_lines"] = _state_line_pairs(rule_lines, rails)
    return out


def render_conflict_check(result: dict) -> str:
    """One block with both quotes of each candidate."""
    hits, states = result.get("hits") or [], result.get("state_lines") or []
    c = result.get("counts") or {}
    total = len(hits) + result.get("more", 0) + len(states)
    lines = [f"Rule check: {total} candidate(s) from {c.get('rules', 0)} active rule(s), "
             f"{c.get('rails', 0)} job or goal text(s), {c.get('skills', 0)} pinned skill(s)."]
    for s in states:
        lines.append(f"- owner.md holds a job's state: \"{s['rule'][:240]}\"; the job row wins.")
        lines += [f"  {where} says: \"{quote[:240]}\"" for where, quote in s["quotes"]]
    for h in hits:
        lines.append(f"- owner rule: \"{h.rule[:240]}\"\n  {h.where} says: \"{h.line[:240]}\" "
                     f"(shares: {', '.join(h.shared[:4])})")
    if result.get("more"):
        lines.append(f"- … +{result['more']} more")
    lines += [f"Note: {n}" for n in result.get("notes") or []]
    lines.append("Advisory only — nothing was changed. A candidate shares words; read both "
                 "quotes before you call it a conflict.")
    return "\n".join(lines)


def rail_instruction_lines(task: str, payload: Optional[dict] = None) -> List[str]:
    """How an owner seat shows a job's prose: as the SCOPED instruction it is."""
    lines = ["instruction (the rail's own — a later owner rule outranks it):"]
    lines += [f"  {l}" for l in (task or "").strip().splitlines()] or ["  (empty)"]
    skills = (payload or {}).get("skills") if isinstance(payload, dict) else None
    if skills:
        lines.append("pinned skills: " + ", ".join(str(s) for s in skills))
    return lines


__all__ = ["SweepHit", "sweep", "render_sweep", "rail_instruction_lines", "MIN_OVERLAP",
           "same_subject_lines", "pinned_by_a_rail", "pinned_skill_ids", "conflict_check",
           "render_conflict_check"]
