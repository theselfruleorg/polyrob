"""Owner rules newer than a cron job's text ride into that job's run as overrides.

Prod 2026-10-05: the owner said "Approve posts with me" at 17:56 and Rob wrote
it to ``owner.md``. At 18:01 the PROMO cron run had that rule in its foundation
context and posted anyway: its task text ("post one; no owner message on a
routine slot") was the specific, recent instruction, and the model followed it.
A job's text is frozen when the job is written; the owner's rules are not. So a
run now OPENS with every active owner rule dated on or after the job was
written, stated as overriding the job where they conflict.

Read-only over the existing seams (``core.instance.load_owner_doc`` for the
ACTIVE doc, ``core.doc_claims`` for the dated lines); no new store. Day
granularity (the stamp's date), so a rule written earlier on the job's own day
is repeated — harmless salience, never a missed rule. Fail-open: no doc, no
date or an unreadable doc adds nothing.
"""
from datetime import datetime
from typing import List, Optional

#: At most this many rules (the newest), each clipped.
MAX_RULES = 8
MAX_RULE_CHARS = 500


def newer_owner_rules(doc_text: str, since: Optional[datetime]) -> List[str]:
    """Active, provenance-stamped rule lines dated on or after ``since``'s day."""
    if not doc_text or since is None:
        return []
    from core.doc_claims import active_rule_lines, stamp_of, strip_stamp
    cutoff = since.date().isoformat()
    out = []
    for line in active_rule_lines(doc_text):
        stamp = stamp_of(line)
        if stamp and stamp >= cutoff:
            out.append((stamp, strip_stamp(line).strip()))
    # Newest first whatever the doc's order (stable within a day), then cap.
    out.sort(key=lambda p: p[0], reverse=True)
    return [r for _s, r in out[:MAX_RULES]]


def owner_rules_overlay(doc_text: str, since: Optional[datetime]) -> str:
    """The block a cron run's task opens with, or ``""`` when nothing is newer."""
    rules = newer_owner_rules(doc_text, since)
    if not rules:
        return ""
    clipped = [r if len(r) <= MAX_RULE_CHARS else r[:MAX_RULE_CHARS - 1] + "…" for r in rules]
    return (f"OWNER RULES NEWER THAN THIS JOB'S TEXT (the job was written "
            f"{since.date().isoformat()}). They OVERRIDE the job text below wherever "
            f"they conflict — follow the owner rule, not the job:\n"
            + "\n".join(f"- {r}" for r in clipped))


def overlay_for_job(job, data_dir: str) -> str:
    """Load the job tenant's ACTIVE owner doc and build the overlay. Never raises."""
    try:
        from core.instance import is_unreadable_note, load_owner_doc, resolve_instance_id
        doc = load_owner_doc(data_dir, job.user_id, resolve_instance_id())
        if not doc or doc.startswith("[BLOCKED") or is_unreadable_note(doc):
            return ""
        return owner_rules_overlay(doc, getattr(job, "created_at", None))
    except Exception:
        return ""
