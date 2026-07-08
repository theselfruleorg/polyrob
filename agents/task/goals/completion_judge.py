"""Goal-completion judge (§3.2, 2026-07-05 handoff).

Division of labor (the intelligence-first design): code supplies FACTS (the
framework-recorded action ledger, names + error status) and a cheap aux MODEL
judges whether the goal's ``payload.acceptance`` was met. The ledger is
EVIDENCE for the judge, never a tripwire — an alternative route to the
acceptance still passes, and there is deliberately NO string-matching on
specific tools or error bodies (owner directive: platform/capability
knowledge lives in the agent's memory/skills, not framework code).

Everything here fails OPEN: a judge error/timeout/missing model returns
``unclear`` (= pass), and the scan returns ``[]`` on any surprise. This
module must never be the reason a legitimately-completed goal blocks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

VERDICT_MET = "met"
VERDICT_UNMET = "unmet"
VERDICT_UNCLEAR = "unclear"
_VERDICTS = {VERDICT_MET, VERDICT_UNMET, VERDICT_UNCLEAR}

_JUDGE_SYSTEM = (
    "You judge whether an autonomous agent's goal run actually met its acceptance "
    "criteria. You have NO tools: you cannot read files, fetch URLs, or inspect "
    "anything beyond the evidence in the message — do not narrate an investigation. "
    "You receive: the acceptance text, the framework-recorded ledger of executed "
    "actions (with error status), and the agent's final message. The ledger is "
    "EVIDENCE — an action that appears there may still have FAILED (marked ERROR), "
    "and success claims in prose are not proof. An acceptance satisfied via an "
    "alternative route (different tool, delegated sub-agent) counts as met. "
    'Use "unmet" ONLY when the evidence clearly shows the acceptance was NOT satisfied '
    "(e.g. the required external action never succeeded and no alternative did). "
    'If the evidence in front of you is insufficient or ambiguous, that IS your answer: "unclear". '
    "Your ENTIRE reply must be exactly one single-line JSON object — no analysis, no "
    'preamble, no code fences: {"verdict": "met"|"unmet"|"unclear", "reason": "<one line>"}'
)

_JUDGE_RETRY_NUDGE = (
    "Your previous reply was not a JSON object. You have no tools and nothing further "
    "to examine — judge from the evidence already given. Reply NOW with exactly one "
    'single-line JSON object and nothing else: {"verdict": "met"|"unmet"|"unclear", '
    '"reason": "<one line>"}'
)


def _action_name(action: Any) -> Optional[str]:
    try:
        dumped = action.model_dump(exclude_none=True)
    except Exception:
        return None
    for key in dumped:
        if key != "interacted_element":
            return key
    return None


def _iter_ledger_lines(orchestrator: Any, max_line_chars: int):
    """Yield one evidence line per executed action, sub-agents labeled."""
    for agent in list((getattr(orchestrator, "agents", None) or {}).values()):
        label = "sub:" if getattr(agent, "_is_sub_agent", False) else ""
        steps = getattr(getattr(agent, "history", None), "history", None) or []
        for step in steps:
            actions = getattr(getattr(step, "model_output", None), "action", None) or []
            results = getattr(step, "result", None) or []
            for i, action in enumerate(actions):
                if action is None:
                    continue
                name = _action_name(action)
                if not name:
                    continue
                result = results[i] if i < len(results) else None
                error = getattr(result, "error", None)
                if error:
                    status = f"ERROR: {str(error)[:max_line_chars]}"
                else:
                    content = getattr(result, "extracted_content", None) or ""
                    status = f"ok: {str(content)[:120]}" if content else "ok"
                yield f"{label}{name} -> {status}"


def build_action_evidence(orchestrator: Any, *, max_lines: int = 80,
                          max_line_chars: int = 200) -> str:
    """The framework-recorded action ledger as judge evidence. Never raises."""
    try:
        lines: List[str] = []
        truncated = False
        for line in _iter_ledger_lines(orchestrator, max_line_chars):
            if len(lines) >= max_lines:
                truncated = True
                break
            lines.append(line)
        if not lines:
            return "(no action ledger available)"
        if truncated:
            lines.append(f"... (ledger truncated at {max_lines} actions)")
        return "\n".join(lines)
    except Exception:
        logger.debug("action evidence build failed", exc_info=True)
        return "(no action ledger available)"


def parse_verdict(data: Any) -> Tuple[str, str]:
    """Normalize judge output to (verdict, reason); anything odd -> unclear."""
    try:
        verdict = str((data or {}).get("verdict", "")).strip().lower()
        reason = str((data or {}).get("reason", "") or "no reason given")
    except Exception:
        return (VERDICT_UNCLEAR, "unparseable judge output")
    if verdict not in _VERDICTS:
        return (VERDICT_UNCLEAR, reason)
    return (verdict, reason)


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*$", re.M)
_VERDICT_FALLBACK_RE = re.compile(r'"verdict"\s*:\s*"(met|unmet|unclear)"', re.I)
_REASON_FALLBACK_RE = re.compile(r'"reason"\s*:\s*"([^"]{0,300})"')


def parse_judge_response(text: Any) -> Tuple[str, str]:
    """Tolerantly parse the judge model's reply into (verdict, reason). Never raises.

    Dedicated parser (prod 2026-07-05): the shared ``extract_json_from_model_output``
    is tuned to the AGENT brain-state schema and RAISES on miss, which made every
    wrapped/malformed judge reply fail open and masked real verdicts. Order: strip
    code fences -> whole-string JSON -> outermost-brace slice -> regex fallback for
    a "verdict" field inside malformed JSON -> unclear.
    """
    if not text or not isinstance(text, str):
        return (VERDICT_UNCLEAR, "empty judge response")
    stripped = _FENCE_RE.sub("", text).strip()
    candidates = [stripped]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start:end + 1])
    for candidate in candidates:
        try:
            return parse_verdict(json.loads(candidate))
        except Exception:
            continue
    m = _VERDICT_FALLBACK_RE.search(text)
    if m:
        r = _REASON_FALLBACK_RE.search(text)
        return (m.group(1).lower(), r.group(1) if r else "no reason parsed")
    return (VERDICT_UNCLEAR, "unparseable judge response")


def _main_agent(orchestrator: Any) -> Optional[Any]:
    for agent in list((getattr(orchestrator, "agents", None) or {}).values()):
        if not getattr(agent, "_is_sub_agent", False):
            return agent
    return None


def build_judge_messages(acceptance: str, evidence: str, final: Optional[str]) -> list:
    from modules.llm.messages import HumanMessage, SystemMessage
    body = (
        f"ACCEPTANCE (definition of done):\n{str(acceptance)[:1000]}\n\n"
        f"EXECUTED ACTIONS (framework-recorded ledger):\n{evidence}\n\n"
        f"AGENT'S FINAL MESSAGE:\n{str(final or '')[:2000]}"
    )
    return [SystemMessage(content=_JUDGE_SYSTEM), HumanMessage(content=body)]


async def judge_goal_completion(task_agent: Any, session_id: Optional[str], goal: Any,
                                final: Optional[str], *,
                                timeout: Optional[float] = None) -> Tuple[str, str]:
    """Judge a completed goal run against ``payload.acceptance``. Fail-open.

    Returns ``(verdict, reason)`` with verdict in {met, unmet, unclear}; any
    error/timeout/missing model yields ``unclear`` so the caller passes the run.
    The judge model rides the existing aux seam (``_provision_aux_llm('judge')``,
    fail-open to the run's main model) and is metered like every aux call.
    """
    try:
        acceptance = ((getattr(goal, "payload", None) or {}).get("acceptance") or "").strip()
        if not acceptance:
            return (VERDICT_UNCLEAR, "no acceptance to judge against")
        orchestrator = task_agent.get_orchestrator(session_id)
        evidence = build_action_evidence(orchestrator)
        agent = _main_agent(orchestrator)
        llm = None
        if agent is not None:
            llm = getattr(agent, "_judge_llm", None)
            if llm is None:
                try:
                    # P2-9: async provisioning — don't block the loop building the client.
                    llm = await agent._provision_aux_llm_async("judge")
                    # P2-8: cache the freshly-provisioned judge client on the agent so a
                    # subsequent goal judge / background review reuses it (and it gets
                    # closed once at session cleanup) instead of leaking one httpx pool
                    # per judged goal.
                    if llm is not None:
                        agent._judge_llm = llm
                except Exception:
                    llm = None
            llm = llm or getattr(agent, "llm", None)
        if llm is None:
            return (VERDICT_UNCLEAR, "no judge model available")
        if timeout is None:
            from agents.task.constants import AutonomyConfig
            timeout = float(AutonomyConfig.goal_judge_timeout_sec())
        msgs = build_judge_messages(acceptance, evidence, final)
        import time as _time
        verdict, reason = VERDICT_UNCLEAR, "unparseable judge response"
        # Up to 2 attempts: some chat models narrate an "investigation" instead of
        # answering (prod 2026-07-05); one corrective nudge recovers most of them.
        for attempt in range(2):
            _t0 = _time.time()
            raw = await asyncio.wait_for(llm.ainvoke(msgs), timeout=timeout)
            try:
                from agents.task.agent.core.aux_metering import meter_aux_llm
                await meter_aux_llm(
                    usage_tracker=getattr(orchestrator, "usage_tracker", None),
                    user_id=getattr(goal, "user_id", None),
                    session_id=session_id or "",
                    agent_id=getattr(agent, "agent_id", "") or "",
                    llm=llm, response=raw, duration_seconds=_time.time() - _t0,
                    component="judge", purpose="goal_completion",
                )
            except Exception:
                logger.debug("judge metering skipped", exc_info=True)
            content = getattr(raw, "content", raw)
            content_str = content if isinstance(content, str) else str(content)
            verdict, reason = parse_judge_response(content_str)
            if reason != "unparseable judge response":
                break
            logger.warning("judge reply unparseable for goal %s (attempt %d); head: %r",
                           getattr(goal, "id", "?"), attempt + 1, content_str[:300])
            if attempt == 0:
                from modules.llm.messages import AIMessage, HumanMessage
                msgs = msgs + [AIMessage(content=content_str[:1000]),
                               HumanMessage(content=_JUDGE_RETRY_NUDGE)]
        logger.info("completion judge for goal %s: %s (%s)",
                    getattr(goal, "id", "?"), verdict, reason[:200])
        return (verdict, reason)
    except Exception as e:
        logger.warning("completion judge failed open for goal %s: %s",
                       getattr(goal, "id", "?"), e)
        return (VERDICT_UNCLEAR, f"judge error (fail-open): {e}")
