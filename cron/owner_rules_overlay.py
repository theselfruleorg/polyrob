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
is repeated — harmless salience, never a missed rule. No doc or no job date adds
nothing; an UNREADABLE doc says so (2026-10-13 review B12) — "no newer rule" and
"could not read the rules" are different facts. Undated rule lines cannot be
placed before or after the job, so the block counts them instead of dropping
them silently, and a cut list says how many it left out.
"""
import re
from datetime import datetime
from typing import List, Optional

#: At most this many rules (the newest), each clipped.
MAX_RULES = 8
#: A-10 (2026-10-14 review): the overlay reads the SAME doc the run's foundation
#: pins as "Owner facts" (``load_owner_doc``, the same cap), so every rule it lists
#: is already in context verbatim. Up to 8 x 500 chars were repeated in every
#: cron task. The overlay now keeps the override instruction and a POINTER per
#: rule (its date and its head), not the rule text again.
POINTER_CHARS = 140
_PROVENANCE_RE = re.compile(r"\s*\[from:[^\]]*\]\s*$")


def _scan(doc_text: str, since: datetime):
    """``(newer, undated)``: newer = ``[(stamp, rule)]`` newest first; undated = count."""
    from core.doc_claims import active_rule_lines, stamp_of, strip_stamp
    cutoff = since.date().isoformat()
    out, undated = [], 0
    for line in active_rule_lines(doc_text):
        stamp = stamp_of(line)
        if not stamp:
            undated += 1
        elif stamp >= cutoff:
            out.append((stamp, strip_stamp(line).strip()))
    # Newest first whatever the doc's order (stable within a day).
    out.sort(key=lambda p: p[0], reverse=True)
    return out, undated


def newer_owner_rules(doc_text: str, since: Optional[datetime]) -> List[str]:
    """Active, provenance-stamped rule lines dated on or after ``since``'s day."""
    if not doc_text or since is None:
        return []
    out, _undated = _scan(doc_text, since)
    return [r for _s, r in out[:MAX_RULES]]


def owner_rules_overlay(doc_text: str, since: Optional[datetime]) -> str:
    """The block a cron run's task opens with, or ``""`` when nothing is newer."""
    if not doc_text or since is None:
        return ""
    out, undated = _scan(doc_text, since)
    if not out:
        return ""
    pointers = []
    for _stamp, rule in out[:MAX_RULES]:
        head = _PROVENANCE_RE.sub("", rule)
        head = head if len(head) <= POINTER_CHARS else head[:POINTER_CHARS - 1] + "…"
        pointers.append(head)
    tail = []
    if len(out) > MAX_RULES:
        tail.append(f"({len(out) - MAX_RULES} more newer owner rule(s) omitted here — "
                    f"they are in the owner doc in your context and apply too.)")
    if undated:
        tail.append(f"({undated} owner-doc line(s) carry no date, so this list cannot "
                    f"tell whether they are newer than the job; they are in your context "
                    f"and apply too.)")
    return (f"OWNER RULES NEWER THAN THIS JOB'S TEXT (the job was written "
            f"{since.date().isoformat()}). They OVERRIDE the job text below wherever "
            f"they conflict — follow the owner rule, not the job. "
            f"Each one's full text is in the Owner facts block in your context:\n"
            + "\n".join(f"- {r}" for r in pointers)
            + ("\n" + "\n".join(tail) if tail else ""))


#: What a run reads when the owner doc exists but cannot be read — never "".
UNREADABLE_OVERLAY = ("OWNER RULES: could not read the owner rules doc for this run "
                      "({why}). Do not assume there is no newer owner rule; when the job "
                      "would act on something the owner may have ruled on, take the "
                      "conservative reading.")


def overlay_for_job(job, data_dir: str) -> str:
    """Load the job tenant's ACTIVE owner doc and build the overlay. Never raises.

    No doc / no job date = ``""``; an unreadable or blocked doc = the
    :data:`UNREADABLE_OVERLAY` line, never the same empty text as "nothing newer"."""
    try:
        from core.instance import is_unreadable_note, load_owner_doc, resolve_instance_id
        doc = load_owner_doc(data_dir, job.user_id, resolve_instance_id())
        if not doc:
            return ""
        if doc.startswith("[BLOCKED"):
            return UNREADABLE_OVERLAY.format(why="the doc is blocked by the identity scan")
        if is_unreadable_note(doc):
            return UNREADABLE_OVERLAY.format(why="the file is unreadable")
        return owner_rules_overlay(doc, getattr(job, "created_at", None))
    except Exception as e:
        return UNREADABLE_OVERLAY.format(why=f"{type(e).__name__}")
