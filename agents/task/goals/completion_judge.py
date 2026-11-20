"""Goal-completion judge (§3.2, §4.3): the run's completion CLAIM read against the
mechanical evidence pack (``RunOutcome``) by a cheap aux model.

Code supplies FACTS (the framework-recorded ledger, artifacts, captured refs, typed
checks) and the model judges. The ledger is EVIDENCE, never a tripwire — an
alternative route still passes, and there is deliberately NO string-matching on
specific tools or error bodies (owner directive: platform/capability knowledge
lives in the agent's memory/skills, not framework code).

Everything here fails OPEN: a judge error/timeout/missing model returns
``unclear``. This module must never be the reason a legitimately-completed goal
blocks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

VERDICT_MET = "met"
VERDICT_UNMET = "unmet"
VERDICT_UNCLEAR = "unclear"
_VERDICTS = {VERDICT_MET, VERDICT_UNMET, VERDICT_UNCLEAR}

_JUDGE_RETRY_NUDGE = (
    "Your previous reply was not a JSON object. You have no tools and nothing further "
    "to examine — judge from the evidence already given. Reply NOW with exactly one "
    'single-line JSON object and nothing else: {"verdict": "met"|"unmet"|"unclear", '
    '"reason": "<one line>"}'
)


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


_EVIDENCE_JUDGE_SYSTEM = (
    "You judge whether an autonomous agent's COMPLETION CLAIM is supported by the "
    "framework-recorded evidence of its run. You have NO tools: you cannot read "
    "files, fetch URLs, or inspect anything beyond the evidence in the message — "
    "do not narrate an investigation. You receive: the goal, the agent's claim "
    "(its done() text), an optional acceptance text, the executed-action ledger "
    "(with error status), the artifact diff (files/outputs the framework actually "
    "recorded), and ids/urls captured from successful tool results. Success claims "
    "in prose are NOT proof; the ledger/artifacts are. An outcome achieved via an "
    "alternative route still counts. "
    'Use "unmet" ONLY when the claim is CONTRADICTED by the evidence (e.g. '
    '"posted the thread" with no successful posting action, "saved the report" '
    "with an empty artifact diff). "
    'Use "met" when the evidence concretely supports the claim. '
    'If the evidence is genuinely insufficient either way, answer "unclear". '
    "Your ENTIRE reply must be exactly one single-line JSON object — no analysis, no "
    'preamble, no code fences: {"verdict": "met"|"unmet"|"unclear", "reason": "<one line>"}'
)


def build_evidence_judge_messages(goal: Any, run: Any) -> list:
    """§4.3: the claim read AGAINST the mechanical evidence pack (RunOutcome)."""
    from modules.llm.messages import HumanMessage, SystemMessage
    payload = (getattr(goal, "payload", None) or {})
    acceptance = str(payload.get("acceptance") or "").strip()
    ev = getattr(run, "evidence", None)
    ledger_lines = list(getattr(ev, "ledger", None) or [])
    ledger = "\n".join(ledger_lines[:80]) or "(no action ledger available)"
    artifacts = getattr(run, "artifacts", None) or []
    art_lines = "\n".join(
        f"- {a.get('path') or a.get('kind')}: {a.get('detail') or a.get('bytes')}"
        for a in artifacts[:20] if isinstance(a, dict)) or "(no artifacts recorded)"
    refs = ", ".join(list(getattr(ev, "captured_refs", None) or [])[:20]) or "(none)"
    checks = getattr(ev, "checks", None) or []
    checks_lines = "\n".join(
        f"- {c.get('type')}: {'ok' if c.get('ok') else 'FAILED'} — {c.get('detail')}"
        for c in checks if isinstance(c, dict)) or "(none)"
    body = (
        f"GOAL:\n{str(getattr(goal, 'title', ''))[:300]}\n"
        f"{str(getattr(goal, 'body', '') or '')[:700]}\n\n"
        + (f"ACCEPTANCE (definition of done):\n{acceptance[:1000]}\n\n" if acceptance else "")
        + f"AGENT'S CLAIM (done() text):\n{str(getattr(run, 'done_text', '') or getattr(run, 'reply_text', ''))[:2000]}\n\n"
        f"EXECUTED ACTIONS (framework-recorded ledger):\n{ledger}\n\n"
        f"ARTIFACTS RECORDED BY THE FRAMEWORK:\n{art_lines}\n\n"
        f"IDS/URLS CAPTURED FROM SUCCESSFUL RESULTS:\n{refs}\n\n"
        f"TYPED ACCEPTANCE-CHECK RESULTS:\n{checks_lines}"
    )
    return [SystemMessage(content=_EVIDENCE_JUDGE_SYSTEM), HumanMessage(content=body)]


async def _invoke_judge(task_agent: Any, session_id: Optional[str], goal: Any,
                        msgs: list, *, purpose: str,
                        timeout: Optional[float] = None) -> Tuple[str, str]:
    """Shared judge invocation: provision the aux model, ask (with one
    corrective retry), meter, parse. Fail-open to ``unclear``. Never raises."""
    try:
        orchestrator = task_agent.get_orchestrator(session_id)
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
            from core.config_policy import AutonomyConfig
            timeout = float(AutonomyConfig.goal_judge_timeout_sec())
        import time as _time
        verdict, reason = VERDICT_UNCLEAR, "unparseable judge response"
        # Up to 2 attempts: some chat models narrate an "investigation" instead of
        # answering (prod 2026-07-05); one corrective nudge recovers most of them.
        for attempt in range(2):
            _t0 = _time.time()
            raw = await asyncio.wait_for(llm.ainvoke(msgs), timeout=timeout)
            try:
                from modules.llm.aux_metering import meter_aux_llm
                await meter_aux_llm(
                    usage_tracker=getattr(orchestrator, "usage_tracker", None),
                    user_id=getattr(goal, "user_id", None),
                    session_id=session_id or "",
                    agent_id=getattr(agent, "agent_id", "") or "",
                    llm=llm, response=raw, duration_seconds=_time.time() - _t0,
                    component="judge", purpose=purpose,
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
        logger.info("completion judge (%s) for goal %s: %s (%s)",
                    purpose, getattr(goal, "id", "?"), verdict, reason[:200])
        return (verdict, reason)
    except Exception as e:
        logger.warning("completion judge failed open for goal %s: %s",
                       getattr(goal, "id", "?"), e)
        return (VERDICT_UNCLEAR, f"judge error (fail-open): {e}")


async def judge_run_outcome(task_agent: Any, session_id: Optional[str], goal: Any,
                            run: Any, *,
                            timeout: Optional[float] = None) -> Tuple[str, str]:
    """§4.3 evidence-grounded completion review — the CLAIM read against the
    mechanical evidence pack (RunOutcome). Runs for autonomous completions
    whether or not an acceptance is set. Fail-open to ``unclear`` (which the
    caller records as done-UNVERIFIED, excluded from learning loops)."""
    try:
        msgs = build_evidence_judge_messages(goal, run)
    except Exception as e:
        logger.warning("evidence judge failed open for goal %s: %s",
                       getattr(goal, "id", "?"), e)
        return (VERDICT_UNCLEAR, f"judge error (fail-open): {e}")
    return await _invoke_judge(task_agent, session_id, goal, msgs,
                               purpose="run_outcome", timeout=timeout)
