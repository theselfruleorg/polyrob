"""Canonical run-outcome envelope (§2, intelligence-stack finalization 2026-07-09).

One typed object assembled ONCE at run end, while the orchestrator is still
resident — consumed by the goal dispatcher, cron runner/delivery, episodes,
notifications and self-wake. Replaces per-consumer string re-extraction from
message history, which is how an honest ``done("OUTCOME: BLOCKED — …")`` was
recorded as a success with result "Processing actions" (goal 58a1385d18bf).

Invariants:
- ``done_text`` comes from the ACTION LEDGER (the done() ActionResult's
  ``extracted_content``), NEVER from a message-history AIMessage.
- Framework placeholder strings and the generic run_session status are
  unrepresentable as results (``result_text()`` filters them).
- A degraded extractor becomes a logged event, not a delivered lie.

Everything here is fail-open: an introspection miss degrades to the empty/zero
value and never fails a finished run.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# run_session status strings that carry no information about the agent's output.
GENERIC_STATUSES = frozenset({
    "session completed successfully",
    "session cancelled by user",
})

# ActionResult.extracted_content values authored by the FRAMEWORK (not the
# agent) — never agent output, never a deliverable result.
FRAMEWORK_PLACEHOLDER_TEXTS = frozenset({
    "Processing actions",
    "Brain state extraction failed",
    "Task marked as complete",
    "Action completed successfully",
    "Message sent to user (non-blocking)",
    "Message sent to user. Task paused - will resume when user responds.",
    "Message sent to user. This surface cannot collect a reply, so the task was not paused.",
})


# ---------------------------------------------------------------------------
# Outcome-line parsing (canonical home; agents.task.goals.context re-exports)
# ---------------------------------------------------------------------------

def extract_outcome_line(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    for line in reversed(text.strip().splitlines()):
        s = line.strip()
        if s.upper().startswith("OUTCOME:"):
            out = s[len("OUTCOME:"):].strip()
            return out or None
    return None


_BLOCKED_RE = re.compile(r"^blocked\b[\s—–:\-]*", re.IGNORECASE)


def parse_blocked_outcome(outcome: Optional[str]) -> Optional[str]:
    """If an extracted OUTCOME is a BLOCKED declaration, return the stated need.

    Accepts em-dash/en-dash/hyphen/colon/whitespace separators and any case; the
    declaration must LEAD the outcome ('blocked' mentioned mid-sentence is prose,
    not a declaration). Returns None for a non-BLOCKED outcome, '' for a bare
    'BLOCKED' with no stated need.
    """
    if not outcome:
        return None
    m = _BLOCKED_RE.match(outcome.strip())
    if not m:
        return None
    return outcome.strip()[m.end():].strip()


# ---------------------------------------------------------------------------
# Ledger readers
# ---------------------------------------------------------------------------

def _main_agents(orchestrator: Any) -> list:
    if orchestrator is None:
        return []
    try:
        agents = list((getattr(orchestrator, "agents", None) or {}).values())
        return [a for a in agents if not getattr(a, "_is_sub_agent", False)]
    except Exception:
        return []


def _steps(agent: Any) -> list:
    try:
        return list(getattr(getattr(agent, "history", None), "history", None) or [])
    except Exception:
        return []


def _action_name(action: Any) -> str:
    try:
        dump = action.model_dump(exclude_unset=True)
        for key in dump:
            if key != "interacted_element":
                return str(key)
    except Exception:
        pass
    return ""


def _agent_text(result: Any) -> str:
    """The agent-authored text of a done() result — '' for framework strings."""
    text = str(getattr(result, "extracted_content", "") or "").strip()
    if not text or text in FRAMEWORK_PLACEHOLDER_TEXTS:
        return ""
    return text


def extract_done_text(orchestrator: Any) -> str:
    """The done() ActionResult's text, read from the action ledger in reverse.

    Position-robust (unlike ``history.final_result()``, which only reads
    ``history[-1].result[-1]``) and immune to message-history placeholders.
    """
    for agent in _main_agents(orchestrator):
        try:
            for step in reversed(_steps(agent)):
                for res in reversed(list(getattr(step, "result", None) or [])):
                    if not getattr(res, "is_done", False):
                        continue
                    text = _agent_text(res)
                    if text:
                        return text
            # History unavailable/empty: the last-step results still carry done().
            for res in reversed(list(getattr(agent, "_last_result", None) or [])):
                if getattr(res, "is_done", False):
                    text = _agent_text(res)
                    if text:
                        return text
        except Exception:
            continue
    return ""


def collect_user_messages(orchestrator: Any, *, limit: int = 50) -> List[str]:
    """send_message texts the agent addressed to its user during the run.

    Read from the ledger (action params paired with a non-error result) —
    the ActionResult content of a send is a framework placeholder.
    """
    out: List[str] = []
    for agent in _main_agents(orchestrator):
        try:
            for step in _steps(agent):
                actions = list(getattr(getattr(step, "model_output", None), "action", None) or [])
                results = list(getattr(step, "result", None) or [])
                for i, action in enumerate(actions):
                    if _action_name(action) != "send_message":
                        continue
                    if i < len(results) and getattr(results[i], "error", None):
                        continue
                    try:
                        params = action.model_dump(exclude_unset=True).get("send_message") or {}
                        text = str(params.get("text") or "").strip()
                    except Exception:
                        text = ""
                    if text:
                        out.append(text)
                        if len(out) >= limit:
                            return out
        except Exception:
            continue
    return out


# Communication actions — not "work"; excluded from the §4.2 all-errored invariant.
_COMMUNICATION_ACTIONS = frozenset({"done", "send_message"})


def all_actions_errored(orchestrator: Any) -> bool:
    """§4.2 invariant input: did EVERY substantive action in the run error?

    A done() on top of nothing but errors is not a judgment call — it is a
    failure. done/send_message are communication, not work, so a pure-chat run
    (no substantive actions at all) returns False.
    """
    saw_substantive = False
    for agent in _main_agents(orchestrator):
        try:
            for step in _steps(agent):
                actions = list(getattr(getattr(step, "model_output", None), "action", None) or [])
                results = list(getattr(step, "result", None) or [])
                for i, action in enumerate(actions):
                    name = _action_name(action)
                    if name in _COMMUNICATION_ACTIONS:
                        continue
                    result = results[i] if i < len(results) else None
                    if result is None:
                        continue
                    saw_substantive = True
                    if not getattr(result, "error", None):
                        return False
        except Exception:
            continue
    return saw_substantive


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------

@dataclass
class RunOutcome:
    """Everything a run's consumers may read — assembled once, typed, honest."""

    session_id: Optional[str]
    status: Optional[str] = None          # raw run_session return (provenance)
    refusal: bool = False                 # run_session returned a known refusal
    done_called: Optional[bool] = None    # tri-state, completed_via_done semantics
    done_text: str = ""                   # done() text from the action ledger
    outcome_line: Optional[str] = None    # parsed trailing "OUTCOME: …"
    blocked: bool = False
    blocked_need: Optional[str] = None
    reply_text: str = ""                  # _extract_chat_reply, display fallback
    user_messages: List[str] = field(default_factory=list)
    artifacts: List[Any] = field(default_factory=list)
    steps: int = 0
    spend_usd: float = 0.0
    evidence: Any = None                  # EvidencePack (§4.1)
    all_actions_errored: bool = False     # §4.2 invariant input
    verified: str = "unverified"          # verified|unverified|failed_verification

    def result_text(self) -> str:
        """Canonical display/record string.

        Priority: done() ledger text → extracted reply → a NON-generic
        run_session return (custom task_agents return real output there).
        Framework placeholders and the generic status are unrepresentable;
        a refusal never surfaces stale text.
        """
        if self.refusal:
            return ""
        for cand in (self.done_text, self.reply_text):
            c = (cand or "").strip()
            if c and c not in FRAMEWORK_PLACEHOLDER_TEXTS:
                return c
        s = (self.status or "").strip()
        if s and s.lower() not in GENERIC_STATUSES:
            return s
        return ""


async def build_run_outcome(task_agent: Any, session_id: Optional[str],
                            status: Optional[str]) -> RunOutcome:
    """Assemble the envelope right after run_session, while the orchestrator
    is still resident. Never raises."""
    from agents.task.runtime.run_as_session import is_refusal, completed_via_done

    outcome = RunOutcome(session_id=session_id, status=(status or None),
                         refusal=is_refusal(status))
    if outcome.refusal:
        return outcome

    orch = None
    try:
        get_orch = getattr(task_agent, "get_orchestrator", None)
        if callable(get_orch):
            orch = get_orch(session_id)
    except Exception:
        orch = None

    outcome.done_called = completed_via_done(orch)
    outcome.done_text = extract_done_text(orch)
    outcome.user_messages = collect_user_messages(orch)

    reply = ""
    try:
        extract = getattr(task_agent, "_extract_chat_reply", None)
        if callable(extract):
            reply = str(extract(session_id) or "").strip()
    except Exception:
        reply = ""
    if reply in FRAMEWORK_PLACEHOLDER_TEXTS:
        reply = ""
    outcome.reply_text = reply

    try:
        from modules.memory.episodic import collect_provenance
        prov = await collect_provenance(orch)
        outcome.steps = int(prov.get("steps", 0) or 0)
        outcome.spend_usd = float(prov.get("spend_usd", 0.0) or 0.0)
    except Exception:
        pass

    # §4.1: the mechanical evidence pack (ledger, artifact diff, final errors,
    # captured refs) + the §4.2 all-errored invariant input.
    try:
        from agents.task.runtime.evidence import build_evidence
        outcome.evidence = build_evidence(orch)
        outcome.artifacts = list(outcome.evidence.artifacts or [])
    except Exception:
        logger.debug("run_outcome: evidence collection failed", exc_info=True)
    try:
        outcome.all_actions_errored = all_actions_errored(orch)
    except Exception:
        pass

    try:
        line = extract_outcome_line(outcome.done_text) or extract_outcome_line(outcome.reply_text)
        if line is None:
            s = (status or "").strip()
            if s and s.lower() not in GENERIC_STATUSES:
                line = extract_outcome_line(s)
        outcome.outcome_line = line
        need = parse_blocked_outcome(line)
        outcome.blocked = need is not None
        outcome.blocked_need = need
    except Exception:
        logger.debug("run_outcome: outcome-line parse failed", exc_info=True)

    # §2: a degraded extractor is a LOGGED EVENT, not a delivered lie.
    if outcome.done_called is True and not outcome.done_text:
        logger.warning(
            "run_outcome: done() completed but no agent text recoverable from the "
            "action ledger (session %s) — extractor degraded", session_id)
        try:
            from agents.task.telemetry.event_log import get_event_log
            get_event_log().record(
                "run_outcome_degraded",
                user_id=str(getattr(orch, "user_id", "") or ""),
                session_id=str(session_id or ""),
                source="run_outcome",
            )
        except Exception:
            pass
    return outcome
