"""The ``rules`` status section (060 WS-7, 2026-09-23): is a written rule IN EFFECT?

Every one of the four 2026-09-21 defects failed SILENT: a rule was written and
nothing said it was not being read. This section reports, per instruction
surface (``core.instruction_surfaces``) where available: active and pending
rules, supersession, last change, and **written but not loaded** health items.
Skill approval and session-loaded counts are explicitly unavailable; pending
drafts alone are not a complete instruction inventory. A pending rule is a health item, so it also reaches
the agent's per-turn ``<live-health>`` note.

No new store: everything is derived from the documents and stores that already
exist, read-only. A store this section cannot read renders its reason; it never
vanishes (``tests/test_status_silence_ratchet.py`` pins this file at zero silent
handlers).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.status_snapshot import SEVERITY_CRIT, SEVERITY_WARN, HealthItem, Section

#: what :func:`_read` returns for a file that exists and cannot be read.
_UNREADABLE = None


def _read(path: Path) -> Optional[str]:
    """The file's text; ``""`` when absent; ``None`` when it exists and is unreadable."""
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return _UNREADABLE  # the caller renders "unreadable" — never a silent zero


def _latest_stamp(text: str) -> str:
    from core.doc_claims import active_rule_lines, stamp_of
    dates = [d for d in (stamp_of(l) for l in active_rule_lines(text)) if d]
    return max(dates) if dates else ""


def _owner_rules(sec: Section, data_dir: str, uid: str, instance_id: str,
                 pending_kinds: Dict[str, List[dict]]) -> None:
    from core.doc_claims import active_rule_lines, superseded_entries
    from core.instance import is_unreadable_note, load_owner_doc, self_tier_root
    raw = _read(self_tier_root(data_dir, uid, instance_id) / "owner.md")
    info: Dict[str, Any] = {"surface": "owner_rules"}
    if raw is None:
        info["state"] = "unreadable"
        sec.lines.append("owner rules: owner.md is unreadable")
        sec.health.append(HealthItem(
            key="rules_owner_unreadable", severity=SEVERITY_CRIT,
            text="owner rules (owner.md) exist but cannot be read — no owner rule is in effect",
            remedy="check the file's permissions under the identity directory"))
        sec.data["owner_rules"] = info
        return
    active, superseded = active_rule_lines(raw), superseded_entries(raw)
    loaded = load_owner_doc(data_dir, uid, instance_id)
    info.update(active=len(active), superseded=len(superseded),
                last_change=_latest_stamp(raw), loaded=bool(loaded) and not loaded.startswith("[BLOCKED")
                and not is_unreadable_note(loaded))
    pending = pending_kinds.get("owner_doc", []) + pending_kinds.get("contract", [])
    info["pending"] = len(pending)
    bits = [f"{len(active)} active"]
    if superseded:
        bits.append(f"{len(superseded)} superseded")
    if info["last_change"]:
        bits.append(f"last change {info['last_change']}")
    if pending:
        bits.append(f"{len(pending)} pending — NOT in effect")
    sec.lines.append("owner rules: " + (" · ".join(bits) if raw.strip() else "none written"))
    # A doc whose active part is empty (everything superseded) loads "" by design.
    if active and not info["loaded"]:
        sec.health.append(HealthItem(
            key="rules_owner_not_loaded", severity=SEVERITY_CRIT,
            text=("owner rules are WRITTEN but NOT LOADED — owner.md failed the load "
                  "guard (identity scan or over the cap), so no session reads them"),
            remedy="open owner.md; shorten it or remove the flagged text, then /status"))
    for item in pending:
        preview = (item.get("preview") or "").strip()[:100]
        sec.health.append(HealthItem(
            key="rule_pending", severity=SEVERITY_WARN,
            text=f"an owner rule is written but NOT in effect yet (pending approval): {preview!r}",
            remedy="/pending — approve it to put it in effect"))
    sec.data["owner_rules"] = info


def _soul(sec: Section, data_dir: str) -> None:
    from core.instance import SELF_CONTEXT_PER_DOC_MAX_CHARS
    base = Path(data_dir) / "identity"
    info: Dict[str, Any] = {"surface": "soul", "docs": {}}
    bits = []
    for name in ("identity.md", "operating.md"):
        text = _read(base / name)
        if text is None:
            info["docs"][name] = "unreadable"
            bits.append(f"{name} unreadable")
            sec.health.append(HealthItem(
                key="rules_soul_unreadable", severity=SEVERITY_WARN,
                text=f"SOUL doc {name} exists but cannot be read — it is not in context",
                remedy="check the file's permissions under <data>/identity/"))
            continue
        if not text.strip():
            continue
        n = len(text.strip())
        info["docs"][name] = n
        bits.append(f"{name} {n} chars")
        if n > SELF_CONTEXT_PER_DOC_MAX_CHARS:
            sec.health.append(HealthItem(
                key="rules_soul_truncated", severity=SEVERITY_WARN,
                text=(f"SOUL doc {name} is {n}/{SELF_CONTEXT_PER_DOC_MAX_CHARS} chars — "
                      "its END is truncated at load and NOT in context"),
                remedy=f"shorten or split <data>/identity/{name}"))
    sec.lines.append("soul: " + (" · ".join(bits) if bits else "none (operator docs absent)"))
    sec.data["soul"] = info


def _self_notes(sec: Section, data_dir: str, uid: str, instance_id: str,
                pending_kinds: Dict[str, List[dict]]) -> None:
    from core.config_policy import AutonomyConfig
    from core.instance import is_unreadable_note, load_self_doc, self_tier_root
    root = self_tier_root(data_dir, uid, instance_id)
    raw = _read(root / "self.md")
    contract = _read(root / "contract.md")
    info: Dict[str, Any] = {"pending": len(pending_kinds.get("self_context", []))}
    if raw is None:
        sec.lines.append("self notes: self.md is unreadable")
        info["state"] = "unreadable"
    elif raw.strip():
        loaded = load_self_doc(data_dir, uid, instance_id)
        info["chars"] = len(raw.strip())
        info["loaded"] = (bool(loaded) and not loaded.startswith("[BLOCKED")
                          and not is_unreadable_note(loaded))
        sec.lines.append(f"self notes: {info['chars']} chars"
                         + ("" if info["loaded"] else " — NOT loaded (blocked at load)"))
        if not info["loaded"]:
            sec.health.append(HealthItem(
                key="rules_self_not_loaded", severity=SEVERITY_WARN,
                text="the agent's self notes (self.md) are written but NOT loaded (load guard)",
                remedy="review self.md; the identity scan or the cap refused it"))
    if contract is None:
        sec.lines.append("contract: contract.md is unreadable")
    elif contract.strip():
        on = AutonomyConfig.contract_doc_enabled()
        info["contract_loaded"] = on
        sec.lines.append("contract: contract.md " + ("in effect" if on else
                                                     "written but NOT loaded (CONTRACT_DOC_ENABLED=false)"))
        if not on:
            from core.remedy import flag_remedy
            sec.health.append(HealthItem(
                key="rules_contract_not_loaded", severity=SEVERITY_WARN,
                text="the operating contract (contract.md) is written but NOT loaded",
                remedy=(flag_remedy("CONTRACT_DOC_ENABLED")
                        + ", or move the rules into owner.md")))
    sec.data["self_notes"] = info


def _skills(sec: Section, pending_kinds: Dict[str, List[dict]]) -> None:
    pending = pending_kinds.get("skill", [])
    sec.data["skills"] = {"surface": "skills", "pending": len(pending),
                          "inventory_state": "unavailable", "approved": None, "loaded": None}
    sec.lines.append("skill drafts: " + (f"{len(pending)} draft(s) pending — NOT in effect"
                                   if pending else "no pending drafts"))
    sec.lines.append("skills: approved and session-loaded counts unavailable")


def _rail_prose(sec: Section, uid: str, cron_db: str) -> None:
    from core.status_snapshot import _rows
    info: Dict[str, Any] = {"surface": "rail_prose"}
    try:
        rows = _rows(cron_db, "SELECT id, task, payload FROM cron_jobs "
                              "WHERE enabled=1 AND user_id=?", (uid,))
    except FileNotFoundError:
        info["state"] = "no cron store"
        sec.lines.append("rail prose: no cron store yet")
        sec.data["rail_prose"] = info
        return
    except Exception as e:
        info["state"] = f"unreadable ({type(e).__name__})"
        sec.lines.append(f"rail prose: cron store unreadable ({type(e).__name__}: {str(e)[:80]})")
        sec.data["rail_prose"] = info
        return
    sizes = sorted(((len(r.get("task") or ""), str(r.get("id") or "")) for r in rows), reverse=True)
    pinned = 0
    for r in rows:
        try:
            payload = json.loads(r.get("payload") or "{}")
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and payload.get("skills"):
            pinned += 1
    total = sum(n for n, _id in sizes)
    info.update(jobs=len(rows), chars=total, pinned_skills=pinned,
                largest=({"id": sizes[0][1], "chars": sizes[0][0]} if sizes else None))
    if rows:
        line = f"rail prose: {len(rows)} enabled job(s) carry {total:,} chars of standing instruction"
        line += f" (largest {sizes[0][0]:,}: {sizes[0][1]})"
        if pinned:
            line += f" · {pinned} pin their skills"
        sec.lines.append(line)
    else:
        sec.lines.append("rail prose: no enabled cron jobs")
    sec.data["rail_prose"] = info


def _goal_prose(sec: Section, uid: str, data_dir: str) -> None:
    """Use the sweep's existing instruction reader for live goal/objective prose."""
    from core.rules_sweep import _rail_texts
    from core.runtime_paths import goals_db_path
    db = goals_db_path(data_dir)
    info = sec.data["rail_prose"]
    if not Path(db).is_file():
        info["goal_state"] = "no goal store"
        sec.lines.append("goal/rail prose: no goal store yet")
        return
    texts, notes = _rail_texts(uid, "", db)
    if notes:
        info["goal_state"] = "; ".join(notes)
        sec.lines.append("goal/rail prose: " + info["goal_state"])
        return
    info["goal_rows"] = len(texts)
    info["goal_chars"] = sum(len(text) for _, text in texts)
    sec.lines.append(f"goal/rail prose: {len(texts)} live goal/objective row(s) carry "
                     f"{info['goal_chars']:,} chars of instruction")


def _workspace_doctrine(sec: Section, uid: str, data_dir: str) -> None:
    """060 WS-3: the persistent workspace's marked documents (``kind:`` front-matter).
    Undeclared documents are RECORDS; only ``kind: instruction`` ones are doctrine."""
    from core.doc_kind import classify_tree, doc_kind_enforced
    info: Dict[str, Any] = dict(classify_tree(str(Path(data_dir) / "workspace")))
    info["surface"] = "workspace_doctrine"
    info["enforced"] = doc_kind_enforced()
    sec.data["workspace_doctrine"] = info
    if info.get("state"):
        sec.lines.append(f"workspace doctrine: {info['state']}")
        return
    line = (f"workspace doctrine: {info['instruction']} instruction doc(s) · "
            f"{info['record']} declared record(s) · {info['undeclared']} undeclared (records)")
    if info.get("unreadable"):
        line += f" · {info['unreadable']} unreadable"
    if not info["enforced"]:
        line += " · record guard OFF"
    sec.lines.append(line)


def _rail_skill_misses(sec: Section, tele: Optional[Section]) -> None:
    """060 WS-5: a rail that PINNED a skill which did not load says so."""
    if tele is None:
        return
    if not tele.available:
        sec.lines.append(f"rail pins: telemetry unreadable ({tele.reason})")
        return
    from core.event_kinds import RAIL_SKILL_MISSING
    rows = [r for r in (tele.data.get("rows") or []) if r.get("kind") == RAIL_SKILL_MISSING]
    if not rows:
        return
    missing = sorted({str(m) for r in rows for m in ((r.get("attrs") or {}).get("missing") or [])})
    sec.data["rail_skill_missing"] = {"runs": len(rows), "skills": missing}
    sec.health.append(HealthItem(
        key="rail_skill_missing", severity=SEVERITY_WARN,
        text=(f"{len(rows)} rail run(s) pinned skill(s) that did NOT load: "
              f"{', '.join(missing[:6])} — they ran on keyword-matched doctrine"),
        remedy="fix the id in the job's `skills` (see /cron show <id>) or author the skill"))


def rules_section(user_id: str, data_dir: str, cron_db: str,
                  tele: Optional[Section] = None) -> Section:
    """Per instruction surface: active · pending · superseded · last change, and
    every rule that is written but NOT in effect as a health item."""
    from core import self_evolution
    from core.instance import resolve_instance_id
    sec = Section(name="rules")
    uid = str(user_id or "")
    instance_id = resolve_instance_id()
    pending_kinds: Dict[str, List[dict]] = {}
    try:
        for it in self_evolution.list_pending(uid, home_dir=data_dir, instance_id=instance_id) or []:
            pending_kinds.setdefault(str(it.get("kind")), []).append(it)
    except Exception as e:
        sec.lines.append(f"pending drafts: unreadable ({type(e).__name__}: {str(e)[:80]})")
    sec.data["system_prompt"] = {"surface": "system_prompt", "state": "code (changes with a deploy)"}
    _owner_rules(sec, data_dir, uid, instance_id, pending_kinds)
    _soul(sec, data_dir)
    _self_notes(sec, data_dir, uid, instance_id, pending_kinds)
    _skills(sec, pending_kinds)
    _rail_prose(sec, uid, cron_db)
    _goal_prose(sec, uid, data_dir)
    _workspace_doctrine(sec, uid, data_dir)
    _rail_skill_misses(sec, tele)
    sec.data["surfaces"] = [k for k in sec.data if isinstance(sec.data.get(k), dict)
                            and sec.data[k].get("surface")]
    return sec


__all__ = ["rules_section"]
