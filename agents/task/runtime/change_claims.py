"""Change claims vs the action ledger — "do not say it was done when it was not".

Prod 2026-10-27/28: an owner asked "have you fixed your jobs?" and the agent
answered "Jobs are fixed" with no ``cronjob_edit`` in the turn; a plan said
"the pinned skill is rewritten to v2" with no ``skill_manage`` call; a run said
"already judged and paid" and, 90 s later, "I did NOT send any money".

This is the doctrine sibling of the ship==tested check (``edit_verify.py``): it
reads the SAME action ledger (``evidence.walk_action_events``), no second
record. At ``done()`` the run loop compares what the turn's words CLAIM was
changed (a job, a skill, the owner doc, money) with the successful writes of
the same kind in this turn. Never a block: the first gap is fed back to the
model for one bounded extra step (do the write, or correct the reply); a gap
that still stands gets one short factual line to the user.

Claim detection is deliberately conservative — a missed claim costs nothing, a
false one costs a step and a wrong note. A sentence counts only when it names
the object, uses a completed-change verb, speaks about now (first person,
"is/are/has been", "now", "done"), and carries no negation, question, plan,
condition or historical marker.

Everything is fail-open: an introspection miss means "no gap".
"""
from __future__ import annotations  # OK here: this is NOT an action-registration module

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set

from agents.task.runtime.evidence import walk_action_events

logger = logging.getLogger(__name__)

JOB, SKILL, OWNER_DOC, MONEY = "job", "skill", "owner_doc", "money"
#: Report order of the categories.
KINDS = (JOB, SKILL, OWNER_DOC, MONEY)

# ---------------------------------------------------------------------------
# Claims (the words)
# ---------------------------------------------------------------------------

_OBJECT = {
    JOB: re.compile(r"\b(cron(?:job)?s?|jobs?)\b", re.I),
    SKILL: re.compile(r"\b(skills?|SKILL\.md)\b", re.I),
    OWNER_DOC: re.compile(r"(\bowner\.md\b|\bowner doc\b|\bstanding rules?\b)", re.I),
}
#: A change verb that names an act even in the passive ("the job is fixed").
#: owner.md keeps this loose rule ("I wrote the new rule to owner.md").
_CHANGE_VERB = re.compile(
    r"\b(edited|updated|changed|fixed|rescheduled|re-?timed|moved|rewritten|rewrote|"
    r"written|wrote|saved|replaced|applied)\b", re.I)
#: A change verb that is about a schedule by itself ("I rescheduled the judge").
_JOB_VERB = re.compile(r"\b(rescheduled|re-?timed)\b", re.I)
#: A verb that also describes a STATE ("the job is scheduled for 18:00", "the
#: repost job is cancelled"): a claim only with an actor or "now/just".
_STATE_VERB = re.compile(
    r"\b(created|added|set up|scheduled|cancell?ed|paused|resumed|removed|deleted|"
    r"recorded)\b", re.I)

# A job or a skill is claimed changed only when it is the verb's OBJECT
# (review 2026-10-29 M1): "edited the judge job", "updated job 18e5…",
# "rewrote the promo-bounty skill", or the passive "the skill is rewritten".
# "saved the digest … for the daily job", "moved on to the next job", "updated
# you on the bounty job", "applied the dca skill", "wrote the post following
# the x-posting skill" and "recorded the winners in the skill notes" are not.
# ("applied" is not a change of the thing applied, so it is not a verb here.)
_DO_VERBS = (r"edited|updated|changed|fixed|rescheduled|re-?timed|moved|rewritten|rewrote|"
             r"written|wrote|saved|replaced")
_STATE_VERBS = r"created|added|set up|scheduled|cancell?ed|paused|resumed|removed|deleted"
_DET = (r"the|a|an|my|your|our|its|his|her|their|this|that|these|those|both|all|each|"
        r"every|two|three|four|five|six|\d+")
#: Words that end the object phrase: a preposition, a pronoun, a determiner or
#: a verb form — the noun after them is not the verb's object.
_STOP = (_DET + r"|to|for|on|onto|in|into|of|with|about|at|by|from|after|before|"
         r"following|via|per|and|or|but|so|then|you|him|them|me|us|it|i|we|is|are|"
         r"was|were|be|been|has|have|had")
_MOD = r"(?:(?!(?:" + _STOP + r")\b)[\w'’-]+\s+)"
_NOUN = {JOB: r"(?:cron(?:job)?s?|jobs?)\b", SKILL: r"(?:skills?\b|SKILL\.md\b)"}


def _direct(verbs: str, noun: str) -> re.Pattern:
    return re.compile(r"\b(?:" + verbs + r")\s+(?:(?:" + _DET + r")\s+)?" + _MOD
                      + r"{0,3}" + noun, re.I)


_DIRECT_DO = {k: _direct(_DO_VERBS, n) for k, n in _NOUN.items()}
_DIRECT_STATE = {k: _direct(_STATE_VERBS, n) for k, n in _NOUN.items()}
_PASSIVE_DO = {k: re.compile(
    n + r"\s+(?:\([^)]*\)\s+)?(?:is|are|was|were|has been|have been)\s+"
    r"(?:(?:now|just|already|all|both|also)\s+)?(?:" + _DO_VERBS + r")\b", re.I)
    for k, n in _NOUN.items()}
#: A terse report that starts with the verb ("Updated the judge job.").
_LEADING_VERB = re.compile(r"^(?:done[:,]?\s+)?(?:" + _DO_VERBS + r")\b", re.I)

#: Money: "paid", or sent/transferred with an amount ("sent 32 PNL", "sent $40"),
#: with I/we as the payer ("I already judged and paid"); "the customer has now
#: paid invoice 12" is incoming, not a payment this turn made. A determiner or
#: another subject between the payer and the verb ends the match.
#: The ticker is case-sensitive: "sent 3 posts" is not money.
_PAYER = (r"(?i:\b(?:i|we)(?:'ve|’ve)?\s+"
          r"(?:(?!(?:the|a|an|this|that|they|he|she|it|you|customer|client)\b)\w+\s+){0,3}?)")
_PAID = re.compile(_PAYER + r"(?i:paid\b)(?![-_])"
                   r"(?!(?i:\s+(?:close\s+)?(?:attention|heed|tribute|respect|a\s+visit)\b))")
#: Upper-case words after a number that are a time zone, a file type or a unit,
#: never a ticker ("sent the digest at 09:00 UTC", "sent you 2 PDF files").
_NOT_A_TICKER = (r"(?!(?:UTC|GMT|CET|CEST|EET|EEST|WET|EST|EDT|CST|CDT|MST|MDT|PST|PDT|BST|"
                 r"ICT|JST|KST|IST|SGT|HKT|AEST|AM|PM|PDFS?|PNGS?|JPE?GS?|GIFS?|CSVS?|"
                 r"DOCX?|XLSX?|ZIPS?|URLS?|APIS?|DMS?|KB|MB|GB|TB|MS|IDS?)\b)")
_SENT_AMOUNT = re.compile(
    _PAYER + r"(?i:(sent|transferred)\b)[^.\n]{0,40}?(\$\s?\d|\b\d[\d,.]*\s*"
    r"((?i:usd|usdc|usdg|eth|sol)|" + _NOT_A_TICKER + r"[A-Z]{2,6})\b)")
#: The sentence speaks about THIS turn: an actor or a now-marker ...
_NOW_ACT = re.compile(r"\b(i|i've|we|we've|now|just|done|already)\b", re.I)
#: ... or a present/perfect passive (only with an act verb).
_NOW_STATE = re.compile(r"\b(is|are|has been|have been)\b", re.I)
#: Negation, plan, condition, question words: never a completed claim.
_NOT_A_CLAIM = re.compile(
    r"(\b(not|no|never|nothing|none|didn't|did not|haven't|hasn't|wasn't|weren't|isn't|"
    r"aren't|can't|cannot|won't|will|i'll|we'll|going to|shall|would|could|should|"
    r"want me|let me|if|once|until|unless|ready to|proposed?|propose|card|cards|draft)\b|\?)",
    re.I)
#: A sentence about an EARLIER turn ("created on 26 Oct", "two days ago").
_HISTORICAL = re.compile(
    r"\b(ago|earlier|yesterday|previously|last (week|time|round|turn)|"
    r"on \d{1,2} [a-z]{3}|on \d{4}-\d{2}-\d{2}|on (mon|tue|wed|thu|fri|sat|sun)[a-z]*)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!])\s+|\n+")


def _sentences(text: str) -> Iterable[str]:
    for s in _SENTENCE.split(text or ""):
        s = s.strip(" \t-*•>")
        if s:
            yield s


def claimed_changes(texts: Iterable[str]) -> Set[str]:
    """The change kinds the texts claim were done now (a subset of :data:`KINDS`)."""
    out: Set[str] = set()
    for text in texts:
        for s in _sentences(text):
            if _NOT_A_CLAIM.search(s) or _HISTORICAL.search(s):
                continue
            acted = bool(_NOW_ACT.search(s))
            if _PAID.search(s) or _SENT_AMOUNT.search(s):
                out.add(MONEY)
            spoken_now = acted or bool(_NOW_STATE.search(s)) or bool(_LEADING_VERB.search(s))
            for kind in (JOB, SKILL):
                if ((spoken_now and _DIRECT_DO[kind].search(s))
                        or (acted and _DIRECT_STATE[kind].search(s))
                        or _PASSIVE_DO[kind].search(s)):
                    out.add(kind)
            if acted and _JOB_VERB.search(s):
                out.add(JOB)
            if (((_CHANGE_VERB.search(s) and (acted or _NOW_STATE.search(s)))
                    or (_STATE_VERB.search(s) and acted))
                    and _OBJECT[OWNER_DOC].search(s)):
                out.add(OWNER_DOC)
    return out


# ---------------------------------------------------------------------------
# Writes (the ledger)
# ---------------------------------------------------------------------------

_JOB_WRITES = frozenset({"cronjob_schedule", "cronjob_edit", "cronjob_cancel",
                         "cronjob_run_now"})
_DOC_READ_ACTIONS = frozenset({"read"})
#: A money result with one of these words moved nothing yet.
_NOT_MOVED = re.compile(r"(approval|pending|queued|nothing (was|is) sent|not sent|"
                        r"simulat|dry.?run|quote)", re.I)


def _money_actions() -> frozenset:
    try:
        from core.verb_policy import ids_where
        return ids_where(effect="money")
    except Exception:
        return frozenset()


def _params(event) -> dict:
    try:
        dumped = event.action.model_dump(exclude_unset=True) or {}
    except Exception:
        return {}
    for value in dumped.values():
        if isinstance(value, dict):
            return value
    return {}


def _ok(event) -> bool:
    return event.result is not None and not getattr(event.result, "error", None)


def _edited_path(event) -> str:
    from agents.task.runtime.edit_verify import _EDIT_ACTIONS, _edit_paths
    if event.name not in _EDIT_ACTIONS:
        return ""
    return " ".join(p for p in _edit_paths(event)).lower()


def _write_kind(event, money: frozenset) -> Optional[str]:
    name = event.name
    if name in _JOB_WRITES:
        return JOB
    if name == "skill_manage":
        return SKILL
    if name == "owner_doc_manage":
        return None if str(_params(event).get("action") or "") in _DOC_READ_ACTIONS else OWNER_DOC
    if name in money:
        text = str(getattr(event.result, "extracted_content", "") or "")
        return None if _NOT_MOVED.search(text) else MONEY
    path = _edited_path(event)
    if path.endswith("owner.md") or "/owner.md " in path + " ":
        return OWNER_DOC
    if "skill.md" in path or "/skills/" in path:
        return SKILL
    return None


def ledger_baseline(orchestrator: Any) -> Dict[str, int]:
    """Action count per agent now — the start of this turn in the ledger."""
    out: Dict[str, int] = {}
    try:
        for event in walk_action_events(orchestrator):
            out[event.agent_id] = event.sequence + 1
    except Exception:
        return {}
    return out


def _turn_events(orchestrator: Any, baseline: Dict[str, int]) -> List[Any]:
    return [e for e in walk_action_events(orchestrator)
            if e.sequence >= baseline.get(e.agent_id, 0)]


def recorded_writes(orchestrator: Any, baseline: Dict[str, int]) -> Set[str]:
    """The change kinds a SUCCESSFUL write of this turn backs."""
    money = _money_actions()
    out: Set[str] = set()
    for event in _turn_events(orchestrator, baseline):
        if _ok(event):
            kind = _write_kind(event, money)
            if kind:
                out.add(kind)
    return out


def turn_words(orchestrator: Any, baseline: Dict[str, int],
               verbs: tuple = ("send_message", "done")) -> List[str]:
    """What the main agent said this turn: its send_message texts and done() texts."""
    out: List[str] = []
    for event in _turn_events(orchestrator, baseline):
        if event.label or event.name not in verbs or not _ok(event):
            continue
        text = str(_params(event).get("text") or "").strip()
        if text:
            out.append(text)
    return out


def unbacked_claims(orchestrator: Any, baseline: Dict[str, int],
                    words_since: Optional[Dict[str, int]] = None) -> List[str]:
    """Claimed change kinds with no successful write this turn, in :data:`KINDS` order.

    ``words_since``: read the claims from this later point (after the nudge) when
    the agent sent the user a new reply there — a corrected reply replaces the
    first one (a done() record alone does not). Fail-open to ``[]``.
    """
    try:
        words = []
        if words_since is not None and turn_words(orchestrator, words_since, ("send_message",)):
            words = turn_words(orchestrator, words_since)
        if not words:
            words = turn_words(orchestrator, baseline)
        claimed = claimed_changes(words)
        if not claimed:
            return []
        backed = recorded_writes(orchestrator, baseline)
        return [k for k in KINDS if k in claimed and k not in backed]
    except Exception:
        logger.debug("change_claims: check skipped (fail-open)", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# What the model and the user are told
# ---------------------------------------------------------------------------

_WRITE_NAME = {
    JOB: "job write (cronjob_schedule / cronjob_edit / cronjob_cancel / cronjob_run_now)",
    SKILL: "skill write (skill_manage)",
    OWNER_DOC: "owner.md write (owner_doc_manage)",
    MONEY: "payment (a money tool that sent funds; a proposed card is not a payment)",
}
_NOT_DONE = {
    JOB: "no job edit was recorded this turn",
    SKILL: "no skill write was recorded this turn",
    OWNER_DOC: "no owner.md write was recorded this turn",
    MONEY: "no payment was sent this turn",
}


def claim_gap_guidance(kinds: List[str]) -> str:
    """The one runtime correction the model gets before the turn ends."""
    names = "; ".join(_WRITE_NAME[k] for k in kinds)
    return ("Your reply says something was changed, but this turn's action ledger has no "
            f"successful {names}. Do the change now with the tool, or send the user a "
            "corrected reply that says what is NOT done. Do not repeat the claim.")


def not_done_line(kinds: List[str]) -> str:
    """The short factual line the user gets when the gap still stands."""
    return "⚠️ Not done: " + "; ".join(_NOT_DONE[k] for k in kinds) + "."


# ---------------------------------------------------------------------------
# The run-loop seam (one call at run start, one at done())
# ---------------------------------------------------------------------------

def start_turn(agent: Any) -> None:
    """Mark this turn's start in the ledger (run start). Never raises."""
    try:
        agent._claim_baseline = ledger_baseline(getattr(agent, "orchestrator", None))
    except Exception:
        agent._claim_baseline = {}
    agent._claim_nudge_at = None


async def check_at_done(agent: Any, *, final: bool = False) -> bool:
    """Run at ``done()``: True = the loop continues for one corrective step.

    First gap: inject :func:`claim_gap_guidance` and continue (once per turn).
    A gap that still stands at the next ``done()``: the user gets
    :func:`not_done_line` and the run ends normally. ``final`` (the run's last
    step: no step is left for a correction) skips the nudge and tells the user
    at once. A sub-agent is never checked (its words go to its parent, not a
    user). Fail-open to False.
    """
    try:
        if getattr(agent, "_is_sub_agent", False):
            return False
        orch = getattr(agent, "orchestrator", None)
        baseline = getattr(agent, "_claim_baseline", None)
        if orch is None or baseline is None:
            return False
        nudged_at = getattr(agent, "_claim_nudge_at", None)
        gaps = unbacked_claims(orch, baseline, words_since=nudged_at)
        if not gaps:
            return False
        if nudged_at is None and not final:
            agent._claim_nudge_at = ledger_baseline(orch)
            from modules.llm.messages import MessageOrigin
            agent.message_manager.inject_runtime_guidance(
                claim_gap_guidance(gaps), origin=MessageOrigin.INTERVENTION,
                source="change_claims")
            logger.info("change_claims: unbacked %s claim(s) — one corrective step", gaps)
            return True
        logger.warning("change_claims: unbacked %s claim(s) after the nudge (session %s)",
                       gaps, getattr(agent, "session_id", ""))
        await _tell_user(agent, orch, not_done_line(gaps))
    except Exception:
        logger.debug("change_claims: done check skipped (fail-open)", exc_info=True)
    return False


async def _tell_user(agent: Any, orch: Any, line: str) -> None:
    """Add ``line`` to this turn's reply, on the path the reply itself took.

    A bound chat (Telegram) or an autonomous run already delivered the reply
    live, so the line goes as its own ``send_message`` (same routing, same
    owner rail). An unbound seat (``chat_once``, raw API, ``/v1``) delivers the
    turn's reply record after the run, so the line is appended to that record.
    Never in a room: the line is about the agent's own bookkeeping.
    """
    from core.surfaces.room_policy import is_public_session
    if is_public_session(orch):
        return
    from core.autonomy_marker import is_autonomous
    bound = bool(getattr(orch, "_message_router", None)
                 and getattr(orch, "_chat_session_key", None))
    if bound or is_autonomous(getattr(agent, "session_id", None)):
        action_model = agent.controller.create_action_model()
        build = getattr(agent, "_build_execution_context", None)
        context = build(None) if callable(build) else None
        await agent.controller.multi_act([action_model(send_message={"text": line})],
                                         execution_context=context)
        return
    from core.surfaces.turn_reply import last_reply_text, mark_reply_published
    prior = (last_reply_text(orch) or "").strip()
    mark_reply_published(orch, f"{prior}\n\n{line}" if prior else line)
