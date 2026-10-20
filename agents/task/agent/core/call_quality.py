"""Model call quality on the agent step — 2026-10-14 review E-2/E-3/E-7/E-8/D-1.

Five different failures (a truly empty completion, a text-only reply, an
unknown tool name, bad tool arguments, a parse failure) used to collapse into
one "Empty action response" journal line, so 779 empty actions could not be
split by cause or by model. This module is the ONE place that:

- names the cause of a tool-free step (``ISSUES``) and records it on the agent
  (``agent._call_issue``), so ``step_execution`` can put it on the log line;
- emits one durable ``model_call_quality`` event per bad call
  (``core.event_log.emit``) with ``{provider, model, regime, issue, session_id,
  step}`` — the per-model call-quality counter the review asked for (E-3);
- repairs a near-miss tool name before the registry sees it (E-7): prefix /
  case / separator repair, the registry's own ``_<name>`` suffix rule, then a
  ``difflib`` match at cutoff 0.7. The repaired name goes through the SAME
  ``tool_calls_to_actions`` → permission → execution path as any call, so no
  gate is skipped;
- decides whether a text-only reply in an AUTONOMOUS run is its final report
  (E-2, owner decision D3) — never on an owner, room, correspondent or child
  seat, where ``send_message`` stays the only reply;
- adds the session ``regime`` to the usage-record metadata (D-1).

Every function is fail-open: telemetry and repair must never break a step.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

EMPTY = "empty"                  # no text and no tool call
TEXT_ONLY = "text_only"          # text, no tool call
UNKNOWN_TOOL = "unknown_tool"    # every call named an action that does not exist
BAD_ARGS = "bad_args"            # every call failed argument validation
PARSE_FAILURE = "parse_failure"  # the response could not be parsed after retries
REPAIRED = "repaired"            # a tool name was repaired (the call then ran)
NO_ACTION = "no_action"          # a tool-free step with no recorded cause
ISSUES = (EMPTY, TEXT_ONLY, UNKNOWN_TOOL, BAD_ARGS, PARSE_FAILURE, REPAIRED, NO_ACTION)

#: The fuzzy-match cutoff (cross-agent parity, review E-7).
FUZZY_CUTOFF = 0.7

#: Wrappers a model puts around a tool name (OpenAI/Gemini habits).
_NAME_PREFIXES = ("functions.", "function.", "default_api.", "tools.", "tool.",
                  "functions:", "default_api:")
_NAME_SUFFIXES = ("_tool", "_action", "_function")

#: A text-only reply shorter than this is not a report (an "ok", a stray token).
_MIN_REPORT_CHARS = 20


# ── regime (D-1) ────────────────────────────────────────────────────────────

def regime_of(agent) -> Optional[str]:
    """``session_class.session_regime`` for *agent*; None on a probe error."""
    try:
        from agents.task.session_class import session_regime
        return session_regime(agent)
    except Exception:
        logger.debug("regime probe failed", exc_info=True)
        return None


def with_regime(metadata: Optional[Dict[str, Any]], agent) -> Dict[str, Any]:
    """*metadata* plus the session ``regime`` — the usage-record key (D-1)."""
    out = dict(metadata or {})
    regime = regime_of(agent)
    if regime:
        out["regime"] = regime
    return out


def stamp_step_context(agent) -> None:
    """D-7: stamp the step's context reading on its LLM right before the call;
    the billing site's ``read_stamps`` copies it into the usage record."""
    try:
        from modules.llm.prefix_stamp import stamp_context
        stamp_context(agent.llm, agent.message_manager.context_usage())
    except Exception:
        logger.debug("context stamp skipped", exc_info=True)


# ── the event (E-3) ─────────────────────────────────────────────────────────

def emit_call_quality(agent, issue: str, **extra: Any) -> None:
    """One ``model_call_quality`` row. Fail-open."""
    try:
        from core.event_kinds import MODEL_CALL_QUALITY
        from core.event_log import emit
        session_id = str(getattr(agent, "session_id", "") or "")
        state = getattr(agent, "state", None)
        attrs = {
            "provider": getattr(agent, "llm_provider", None) or "unknown",
            "model": getattr(agent, "model_name", None) or "unknown",
            "regime": regime_of(agent),
            "issue": issue,
            "session_id": session_id,
            "step": getattr(state, "n_steps", None),
        }
        attrs.update(extra)
        emit(MODEL_CALL_QUALITY, source="agent_step",
             user_id=str(getattr(agent, "user_id", "") or ""),
             session_id=session_id, attrs=attrs)
    except Exception:
        logger.debug("model_call_quality emit skipped", exc_info=True)


def set_call_issue(agent, issue: Optional[str]) -> None:
    """Record the cause of THIS call's tool-free outcome (None = reset)."""
    try:
        agent._call_issue = issue
    except Exception:
        pass


def take_call_issue(agent) -> str:
    """The recorded cause, consumed (so a later step never re-reads it)."""
    issue = getattr(agent, "_call_issue", None) or NO_ACTION
    set_call_issue(agent, None)
    return issue


# ── response shape (E-8 / E-2) ──────────────────────────────────────────────

def response_text(response) -> str:
    """The text of a provider response, string or content blocks."""
    content = getattr(response, "content", None)
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(getattr(block, "text", None), str):
                parts.append(block.text)
        return "".join(parts)
    return str(content)


def is_truly_empty(response) -> bool:
    """No tool call and no text at all — the E-8 in-call retry case."""
    if response is None:
        return False
    return not getattr(response, "tool_calls", None) and not response_text(response).strip()


def _is_brain_only(text: str) -> bool:
    """A JSON object (the brain-state envelope) is a planning turn, not a report."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", stripped).strip()
    if not stripped.startswith("{"):
        return False
    try:
        return isinstance(json.loads(stripped), dict)
    except Exception:
        # An unparseable JSON-looking blob is not prose either.
        return True


_AUTONOMOUS_REGIMES = frozenset({"cron", "goal", "autonomous"})


def prose_final_report(agent, response) -> Optional[Dict[str, Any]]:
    """E-2 (owner decision D3): in a cron / goal / autonomous run, a text-only
    reply with real content IS the final report — return a ``done(text=…)``
    tool call for it; None everywhere else.

    Never on an owner chat, room, correspondent or child seat (``send_message``
    stays the only reply there). Only once the run has ACTED: a prose opening
    before any tool ran is a plan, not a report, and ending the run on it would
    turn every planning turn into a no-op run (38 % of DeepSeek cron runs open
    with one — review C-1). A brain-state JSON reply is a planning turn too.
    """
    try:
        if getattr(response, "tool_calls", None):
            return None
        if not getattr(agent, "_acted_this_run", False):
            return None
        text = response_text(response).strip()
        if len(text) < _MIN_REPORT_CHARS or _is_brain_only(text):
            return None
        if regime_of(agent) not in _AUTONOMOUS_REGIMES:
            return None
        step = getattr(getattr(agent, "state", None), "n_steps", 0)
        return {"id": f"text_final_{step}", "name": "done", "args": {"text": text},
                "type": "tool_call"}
    except Exception:
        logger.debug("prose-final-report check skipped", exc_info=True)
        return None


# ── tool-name repair (E-7) ──────────────────────────────────────────────────

def _snake(name: str) -> str:
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    name = re.sub(r"[\s\-./:]+", "_", name.strip().strip("'\"`"))
    return re.sub(r"_+", "_", name).strip("_").lower()


def repair_tool_name(name: str, known: Iterable[str]) -> Optional[str]:
    """The registered name *name* most plausibly meant, or None.

    Order: exact → wrapper prefix → case/separator → ``_tool`` suffix → the
    registry's own ``<x>_<name>`` suffix rule → ``difflib`` at cutoff 0.7.
    """
    known_list = [k for k in known if isinstance(k, str) and k]
    known_set = set(known_list)
    if not isinstance(name, str) or not name.strip() or not known_set:
        return None
    if name in known_set:
        return name
    cand = name.strip().strip("'\"`")
    for prefix in _NAME_PREFIXES:
        if cand.lower().startswith(prefix):
            cand = cand[len(prefix):]
            break
    if cand in known_set:
        return cand
    cand = _snake(cand)
    if cand in known_set:
        return cand
    for suffix in _NAME_SUFFIXES:
        if cand.endswith(suffix) and cand[: -len(suffix)] in known_set:
            return cand[: -len(suffix)]
    suffixed = [k for k in known_list if k.endswith(f"_{cand}")]
    if suffixed:
        return suffixed[0]
    # The fuzzy step never lands on a money / high-impact action: there 0.7 is a
    # DIFFERENT verb, not a typo (launchpad_claim -> launchpad_sell, email_move ->
    # email_send, defi_trade_lp_add -> defi_trade_swap). Such a call keeps its
    # name and gets the registry's refusal, which names the real way.
    close = difflib.get_close_matches(
        cand, [k for k in known_list if not _fuzzy_blocked(k)], n=1, cutoff=FUZZY_CUTOFF)
    return close[0] if close else None


def _fuzzy_blocked(action_name: str) -> bool:
    """True when *action_name* belongs (longest tool-id prefix) to a money or
    high-impact tool (``core.tool_capabilities``). Fail-closed: True on error."""
    try:
        from core.tool_capabilities import TOOL_CAPABILITIES, ids_with
        owner = None
        for tid in TOOL_CAPABILITIES:
            if (action_name == tid or action_name.startswith(tid + "_")) and (
                    owner is None or len(tid) > len(owner)):
                owner = tid
        return owner is not None and owner in (ids_with("money") | ids_with("high_impact"))
    except Exception:
        return True


def repair_tool_call_names(agent, tool_calls: List[Dict[str, Any]]) -> int:
    """Rewrite each unknown ``name`` in *tool_calls* to its repaired registered
    name (in place, before the tracker and the registry see it). Returns the
    number repaired; emits one ``repaired`` event per call."""
    try:
        controller = getattr(agent, "controller", None)
        known = list(controller.list_actions()) if controller else []
    except Exception:
        known = []
    if not known:
        return 0
    known_set = set(known)
    repaired = 0
    for call in tool_calls or []:
        name = call.get("name")
        if not name or name in known_set:
            continue
        fixed = repair_tool_name(name, known)
        if fixed and fixed != name:
            call["name"] = fixed
            repaired += 1
            try:
                agent.logger.info(f"Tool name repaired: '{name}' -> '{fixed}'")
            except Exception:
                pass
            emit_call_quality(agent, REPAIRED, requested=str(name)[:80], repaired_to=fixed)
    return repaired


def note_rejected_calls(agent, tool_calls: List[Dict[str, Any]], actions: list) -> None:
    """Name the cause of rejected calls (E-3). All rejected → the step's cause
    (``step_execution`` logs and emits it); some rejected → the valid ones still
    run, and one event per rejected call is emitted here."""
    try:
        controller = getattr(agent, "controller", None)
        errors = dict(controller.get_last_validation_errors() or {}) if controller else {}
        known = set(controller.list_actions()) if controller else set()
    except Exception:
        errors, known = {}, set()
    rejected = [tc for tc in tool_calls or [] if tc.get("id") in errors]
    if not rejected and actions:
        return
    if not actions:
        unknown = any(tc.get("name") not in known for tc in (rejected or tool_calls or []))
        set_call_issue(agent, UNKNOWN_TOOL if unknown else BAD_ARGS)
        return
    for tc in rejected:
        issue = UNKNOWN_TOOL if tc.get("name") not in known else BAD_ARGS
        emit_call_quality(agent, issue, partial=True, tool=str(tc.get("name"))[:80])
