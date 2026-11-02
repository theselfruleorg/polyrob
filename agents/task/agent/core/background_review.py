"""Post-turn background-review fork (W2-C, Reference-parity self-improvement loop).

After a run of productive turns, the agent forks a cheap, fire-and-forget sub-agent
that reviews what just happened and — when it finds a durable, reusable procedure —
authors a skill (via the W2 ``skill_manage`` tool, which quarantines it for review).
This is the "learning" loop: experience → distilled skill → available next session.

Design:
- **Non-blocking** — ``asyncio.create_task`` so the run loop returns immediately; a
  review never delays the user-facing turn.
- **Cheap** — runs on the aux/judge model (``_provision_aux_llm("judge")``) when one
  resolves. The child is a fresh sub-agent session: it builds its OWN system prompt
  and does NOT share the parent's cached prefix or see the parent's messages.
- **Grounded** — because the child sees nothing of the parent, the task carries a
  bounded digest of the parent's recent steps (``build_review_digest``: last
  ``_DIGEST_MAX_STEPS`` steps, ``_DIGEST_MAX_CHARS`` chars, framed as untrusted DATA).
- **Least-privilege** — spawned through ``SubAgentManager.run_subtask`` as a depth-1
  LEAF (UP-05 child controller: no code-exec/cron, cannot re-delegate).
- **Bounded** — at most ``BG_REVIEW_MAX_STEPS`` steps; exempt for sub-agents (a
  reviewer never forks its own reviewer).
- **Fail-open** — any error is swallowed; the loop must never break on a review.

Gated ``BACKGROUND_REVIEW_ENABLED`` (default OFF). Authored skills are quarantined
(``created_by="background_review"`` → always ``.pending/``), so even a compromised
review cannot self-activate a skill.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_REVIEW_PROMPT = (
    "You are a background reviewer for an AI agent. The agent just finished a run of "
    "work in this session. Review the digest of its recent steps below (it is all "
    "you see of that work) and decide whether a "
    "DURABLE, REUSABLE procedure emerged that would help in future sessions.\n\n"
    "If yes: call skill_manage(action='create', skill_id=<short-kebab-id>, "
    "when=<one line of <= 60 chars: when to load it>, "
    "content=<a concise SKILL.md: a '# Title' heading then when-to-use + steps>). "
    "Keep it general (no secrets, no session-specific data). Author at most ONE skill.\n"
    "First search the skill index: when a skill already covers it, load_skill it and "
    "patch the smallest span instead of creating one. A rule the owner stated is not "
    "a procedure: skip it.\n"
    "If nothing is worth saving: call done() with a one-line reason. Do not do any "
    "other work, browse, or message the user."
)

_DIGEST_MAX_STEPS = 8
_DIGEST_MAX_CHARS = 4000
_DIGEST_FIELD_CHARS = 240


def _clip(text, n: int = _DIGEST_FIELD_CHARS) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def build_review_digest(history_items, *, max_steps: int = _DIGEST_MAX_STEPS,
                        max_chars: int = _DIGEST_MAX_CHARS) -> str:
    """A bounded, plain-text digest of the parent's last *max_steps* steps: the
    step's memory/next goal, the actions it called, and a clipped result or error.
    Pure; ``""`` when there is nothing to show. Never raises on odd items."""
    lines: list[str] = []
    items = list(history_items or [])[-max(1, int(max_steps)):]
    for i, item in enumerate(items, 1):
        try:
            out = getattr(item, "model_output", None)
            brain = getattr(out, "current_state", None)
            parts = []
            memory = getattr(brain, "memory", "") if brain is not None else ""
            if memory:
                parts.append(f"memory: {_clip(memory)}")
            names = []
            for act in (getattr(out, "action", None) or []):
                try:
                    dumped = act.model_dump(exclude_none=True)
                    names.extend(k for k, v in dumped.items() if v is not None)
                except Exception:
                    continue
            if names:
                parts.append("actions: " + ", ".join(names[:6]))
            for res in (getattr(item, "result", None) or [])[:2]:
                err = getattr(res, "error", None)
                content = getattr(res, "extracted_content", None)
                if err:
                    parts.append(f"error: {_clip(err, 160)}")
                elif content:
                    parts.append(f"result: {_clip(content, 160)}")
            if parts:
                lines.append(f"step {i}: " + " | ".join(parts))
        except Exception:
            continue
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = "…" + text[-(max_chars - 1):]  # keep the most recent steps
    return text


def build_review_prompt(digest: str = "", *, base: str = _REVIEW_PROMPT) -> str:
    """The reviewer task plus the parent's step digest (framed as untrusted DATA —
    tool results carry third-party text) when given. The reviewer writes skills
    only: it never proposes an owner or self document (095 W10 step 3, 078 D7)."""
    prompt = base
    if digest:
        try:
            from core.security.untrusted_wrap import wrap_untrusted
            framed = wrap_untrusted("parent_step_digest", digest)
        except Exception:
            framed = digest
        prompt += "\n\nRECENT STEPS OF THE AGENT YOU REVIEW:\n" + framed
    else:
        prompt += ("\n\nNo step digest is available for this run: if you cannot see "
                   "a reusable procedure, call done() with that reason.")
    return prompt


#: 078 D7: the reviewer writes skills only — its child registry never holds the
#: owner or self document writers (enforced in code, not only in the prompt).
REVIEWER_EXCLUDED_ACTIONS = frozenset({"owner_doc_manage", "self_context_manage"})


def _prefs_home_dir() -> str:
    """Data-home for pref resolution (seam kept for test monkeypatching)."""
    from core.runtime_paths import data_dir_or_home
    return data_dir_or_home(None)


def effective_background_review_enabled(user_id, home_dir) -> bool:
    """Tenant-effective background-review switch: the ``BACKGROUND_REVIEW_ENABLED``
    env/posture default AND-merged with the ``autonomy.background_review`` pref —
    the pref can only DISABLE the loop, never enable one the operator has off
    (018 P0.2; this key was DEAD: settable/displayed but the mixin read the env
    directly). No pref file present => byte-identical to
    ``AutonomyConfig.background_review_enabled()``. Fail-open to the env value."""
    from core.config_policy import AutonomyConfig
    from core.prefs import effective_autonomy_switch
    return effective_autonomy_switch("autonomy.background_review",
                                     AutonomyConfig.background_review_enabled(),
                                     user_id, home_dir)


def _review_gates_open(user_id) -> bool:
    """The switches every reviewer shares: ``BACKGROUND_REVIEW_ENABLED`` (pref
    can only disable), ``SKILLS_WRITABLE`` (the reviewer's only output rail —
    SK-F8: firing it read-only burns an aux run that fails at the tool layer)
    and the owner pause (031)."""
    from core.config_policy import AutonomyConfig
    if not effective_background_review_enabled(user_id, _prefs_home_dir()):
        return False
    if not AutonomyConfig.skills_writable():
        return False
    from core.autonomy_control import allows
    return bool(allows("background_review").allowed)


class BackgroundReviewMixin:
    """Fire a post-turn self-improvement reviewer. Composed into Agent."""

    def _bg_review_should_fire(self, turn_was_productive: bool) -> bool:
        """Pure decision: increment the productive-turn counter and report whether the
        interval has elapsed. Resets the counter when it fires."""
        from core.config_policy import AutonomyConfig
        if not _review_gates_open(getattr(self, "user_id", None)):
            return False
        if getattr(self, "_is_sub_agent", False):
            return False  # a reviewer never forks a reviewer
        # The inline per-turn reviewer is OWNER CHAT only. A goal / cron run is
        # unverified mid-run (the judge and the receipts come at its end): it is
        # reviewed AFTER the run by ``review_after_run`` (095 W13, the
        # ``RegimePolicy.review_after_run`` column). A leaf, a room and a
        # correspondent session never distil third-party text into a skill.
        try:
            from agents.task.session_class import OWNER_CHAT, session_regime
            if session_regime(self) != OWNER_CHAT:
                return False
        except Exception:
            return False
        if not turn_was_productive:
            return False
        interval = max(1, AutonomyConfig.bg_review_interval())
        n = getattr(self, "_bg_review_productive_turns", 0) + 1
        if n >= interval:
            self._bg_review_productive_turns = 0
            return True
        self._bg_review_productive_turns = n
        return False

    def _maybe_spawn_background_review(self, *, turn_was_productive: bool) -> None:
        """Fire-and-forget the reviewer if the cadence says so. Never blocks/raises."""
        try:
            if not self._bg_review_should_fire(turn_was_productive):
                return
            task = asyncio.create_task(self._run_background_review())
            # M5: keep a strong reference so CPython can't GC the task mid-run (a task no
            # one references may be collected before it completes), and so it's cancelled
            # at session teardown. Mirrors _stall_check_task; self-removes when done.
            _tasks = getattr(self.orchestrator, "_execution_tasks", None)
            if isinstance(_tasks, list):
                _tasks.append(task)
                task.add_done_callback(lambda t, _l=_tasks: (_l.remove(t) if t in _l else None))
            logger.info("🪞 spawned background-review fork (session %s)",
                        getattr(self.orchestrator, "session_id", "?"))
        except Exception as e:
            logger.debug("background-review spawn skipped: %s", e)

    async def _run_background_review(self) -> None:
        """Run the reviewer sub-agent on the aux model. Fail-open."""
        try:
            from core.config_policy import AutonomyConfig
            manager = self.orchestrator.get_sub_agent_manager() if self.orchestrator else None
            if manager is None:
                return
            # P2-8: reuse the agent's cached judge client instead of building a fresh
            # isolated client (with its own httpx pool) on every fire — the old code
            # leaked one pool per background review. Cached on self._judge_llm (the same
            # slot _validate_output uses) and closed once at session cleanup (M2).
            aux = getattr(self, "_judge_llm", None)
            if aux is None:
                try:
                    # P2-9: async provisioning — don't block the loop building the client.
                    aux = await self._provision_aux_llm_async("judge")
                    if aux is not None:
                        self._judge_llm = aux
                except Exception:
                    aux = None
            # P2-10: make the reviewer's model cost VISIBLE. On openrouter the cheap aux
            # model keys on the MAIN model's family (OPENROUTER_AUX_FAMILY_MAP, review
            # B-2: z-ai/glm-5* -> z-ai/glm-5.3-flash); a family with no row (deepseek,
            # whose flash is already the cheap tier) and nvidia resolve to None, and then
            # this PROACTIVE loop (fires every BG_REVIEW_INTERVAL turns without any user
            # ask) runs up to bg_review_max_steps on the main model. Log at INFO which
            # model it will use so the cost isn't silent (set AUX_MODEL_JUDGE to route
            # it elsewhere).
            _review_llm = aux or self.llm
            if aux is None:
                logger.info(
                    "background review running on the MAIN model %r (no cheap aux "
                    "resolved) — set AUX_MODEL_JUDGE or add a provider cheap-map entry "
                    "to route it to a cheaper model",
                    getattr(self.llm, "model_name", getattr(self.llm, "model_type", "?")),
                )
            try:
                _hist = getattr(getattr(self, "history", None), "history", None)
                digest = build_review_digest(_hist)
            except Exception:
                digest = ""
            await manager.run_subtask(
                task=build_review_prompt(digest),
                parent_agent_id=self.agent_id,
                profile_id="executor",
                max_steps=AutonomyConfig.bg_review_max_steps(),
                parent_llm=_review_llm,
                skip_complexity_check=True,
                exclude_actions=set(REVIEWER_EXCLUDED_ACTIONS),
            )
        except Exception as e:
            logger.debug("background-review run failed (ignored): %s", e)


# --------------------------------------------------------------------------- #
# 095 W13: the reviewer AFTER a goal / cron run, on a trusted outcome signal.
# --------------------------------------------------------------------------- #

#: R1: learn only from a signal that is not the agent's own opinion.
TRUSTED_SIGNALS = ("receipt", "owner", "judge")

_RUN_REVIEW_PROMPT = (
    "You are a background reviewer for an AI agent. One of its scheduled runs just "
    "ended. You see the run's outcome record and a digest of its last steps; that is "
    "all you see of that work. Decide whether the run teaches ONE durable lesson that "
    "would make the next run of this kind better (a failed run: the step that would "
    "have prevented the failure).\n\n"
    "Write at most ONE change, in this order of preference:\n"
    "(a) PATCH a skill the run loaded: load_skill(<id>) to get its sha256, then "
    "skill_manage(action='patch', skill_id=<id>, expected_sha256=<sha256>, "
    "old_string=<the smallest span>, new_string=<that span plus one bullet>).\n"
    "(b) If no loaded skill fits, patch the same way the first pinned skill of the "
    "run, with one lesson line.\n"
    "(c) Only if neither fits: skill_manage(action='create', skill_id=<short-kebab-id>, "
    "when=<one line of <= 60 chars>, content=<a '# Title' heading, when to use, steps>) "
    "— search the skill index first and never duplicate a skill.\n"
    "Keep it general: no secrets, no ids, no amounts, no dates of this run. Every "
    "write is a draft that the owner reviews. Write skills only: do not edit owner or "
    "self documents, do not message anyone, do not do the run's work again.\n"
    "If there is no durable lesson: call done() with a one-line reason."
)


def review_after_run_due(regime: str, meta) -> bool:
    """Pure: does this run record earn a review after the run? The regime must
    have ``RegimePolicy.review_after_run``; the record's ``signal_source`` must
    be trusted (receipt | owner | judge — never self or none); a GOAL run needs
    a judge verdict or a receipt, a CRON run a useful outcome or a failure with
    a classified stop reason. Never raises."""
    try:
        from agents.task.session_class import CRON, GOAL, REGIME_POLICY
        policy = REGIME_POLICY.get(regime)
        if policy is None or not policy.review_after_run:
            return False
        m = meta if isinstance(meta, dict) else {}
        if m.get("signal_source") not in TRUSTED_SIGNALS:
            return False
        if regime == GOAL:
            return bool(str(m.get("verified") or "").strip()) or bool(m.get("useful"))
        if regime == CRON:
            return bool(m.get("useful")) or _classified_failure(m.get("stop_reason"))
    except Exception:
        logger.debug("review_after_run_due: probe failed", exc_info=True)
    return False


def _classified_failure(stop_reason) -> bool:
    """A typed stop reason that names a failure (not done / a pause / an owner stop)."""
    from agents.task.runtime.stop_reason import QUIET_STOPS, StopReason
    value = str(stop_reason or "")
    return value in {r.value for r in StopReason} and value not in QUIET_STOPS


def run_outcome_line(outcome: str, meta) -> str:
    """The W11 outcome line the reviewer reads, with ``id@g<patch generation>``."""
    m = meta if isinstance(meta, dict) else {}
    gens = m.get("skill_gens") or {}
    ids = [str(s) for s in (m.get("skills_loaded") or [])][:6]
    skills = ",".join(f"{s}@g{gens[s]}" if gens.get(s) else s for s in ids) or "-"

    def _v(key):
        v = m.get(key)
        if isinstance(v, bool):
            return "yes" if v else "no"
        return str(v) if v not in (None, "") else "-"

    return (f"outcome={outcome or '?'} verified={_v('verified')} useful={_v('useful')} "
            f"stop={_v('stop_reason')} signal={_v('signal_source')} skills={skills}")


def build_run_review_prompt(digest: str, *, outcome_line: str,
                            pinned: list = None) -> str:
    """The after-run reviewer task: the outcome line, the loaded skills with their
    patch generation, the run's first pinned skill, and the step digest (DATA)."""
    head = _RUN_REVIEW_PROMPT + "\n\nRUN OUTCOME: " + outcome_line
    pins = [str(p) for p in (pinned or []) if str(p).strip()]
    head += "\nFIRST PINNED SKILL OF THIS RUN: " + (pins[0] if pins else "(none)")
    return build_review_prompt(digest, base=head)


def _main_agent(orchestrator):
    for agent in list((getattr(orchestrator, "agents", None) or {}).values()):
        if not getattr(agent, "_is_sub_agent", False):
            return agent
    return None


def _drafts_since(user_id, since_ts: float) -> list:
    """The skill drafts the reviewer wrote: ``self_modification`` events of
    provenance ``background_review`` since *since_ts*. ``[]`` when unreadable."""
    try:
        from core.event_kinds import SELF_MODIFICATION
        from core.event_log import event_log_enabled, open_event_log
        if not event_log_enabled():
            return []
        log = open_event_log(None)
        if log is None:
            return []
        out = []
        for ev in log.query(kind=SELF_MODIFICATION, user_id=str(user_id or ""),
                            since_ts=since_ts, limit=50) or []:
            a = ev.get("attrs") or {}
            if (a.get("created_by") == "background_review" and a.get("ok")
                    and a.get("pending") and a.get("action") in ("create", "patch", "edit")):
                item = f"{a.get('action')} {a.get('item_id')}"
                if item not in out:
                    out.append(item)
        return out
    except Exception:
        logger.debug("reviewer drafts unreadable", exc_info=True)
        return []


async def review_after_run(task_agent, *, regime: str, session_id, user_id,
                           rail_id: str, outcome: str, meta, pinned=None) -> bool:
    """Run the reviewer on a finished run's resident session. The run has ended,
    so this spends nothing of its step budget: the child gets
    ``BG_REVIEW_MAX_STEPS`` on the aux (judge) model, and its usage rolls up to
    the run's session (``sub_agent_aggregate``), i.e. to that job's economics.
    After a review that drafted something, ONE owner line names the draft (a
    ``self_modification`` ``propose`` event, read by ``core.recap``).
    True when a reviewer ran. Fail-open; never raises."""
    import time
    try:
        if not session_id or not review_after_run_due(regime, meta):
            return False
        if not _review_gates_open(user_id):
            return False
        from core.config_policy import AutonomyConfig
        orch = task_agent.get_orchestrator(session_id)
        agent = _main_agent(orch)
        manager = orch.get_sub_agent_manager() if orch is not None else None
        if agent is None or manager is None:
            return False
        aux = getattr(agent, "_judge_llm", None)
        if aux is None:
            try:
                aux = await agent._provision_aux_llm_async("judge")
                if aux is not None:
                    agent._judge_llm = aux  # closed once at session cleanup (M2)
            except Exception:
                aux = None
        try:
            digest = build_review_digest(getattr(getattr(agent, "history", None),
                                                 "history", None))
        except Exception:
            digest = ""
        started = time.time()
        await manager.run_subtask(
            task=build_run_review_prompt(digest, outcome_line=run_outcome_line(outcome, meta),
                                         pinned=pinned),
            parent_agent_id=agent.agent_id,
            profile_id="executor",
            max_steps=AutonomyConfig.bg_review_max_steps(),
            parent_llm=aux or agent.llm,
            skip_complexity_check=True,
            exclude_actions=set(REVIEWER_EXCLUDED_ACTIONS),
        )
        drafts = _drafts_since(user_id, started - 1)
        if drafts:
            from core.self_events import emit_self_modification
            emit_self_modification(
                kind="skill", action="propose", item_id=drafts[0].split(" ", 1)[-1],
                user_id=str(user_id or ""), session_id=str(session_id),
                pending=True, created_by="background_review", source="background_review",
                note=(f"the reviewer of {rail_id} drafted " + "; ".join(drafts[:3])
                      + " — /pending to review"),
                rail=rail_id, lane=str((meta or {}).get("lane") or ""))
        logger.info("🪞 after-run review of %s done (%d draft(s))", rail_id, len(drafts))
        return True
    except Exception as e:
        logger.debug("after-run review skipped (%s): %s", rail_id, e)
        return False


#: Users with an after-run reviewer in flight. A burst of runs spawns at most ONE
#: reviewer per user at a time (the rest are skipped and logged); the sub-agent
#: semaphore (MAX_CONCURRENT_SUB_AGENTS) still bounds the process.
_REVIEWS_IN_FLIGHT: set = set()


async def _review_after_run_once(key: str, task_agent, **kw) -> bool:
    try:
        return await review_after_run(task_agent, **kw)
    finally:
        _REVIEWS_IN_FLIGHT.discard(key)


def schedule_review_after_run(task_agent, **kw) -> None:
    """Fire-and-forget :func:`review_after_run` (never inside the run's budget or
    its scheduler's time cap). A cheap pure gate first, so a run that does not
    qualify spawns nothing; a user whose reviewer is still running is skipped.
    Never raises."""
    try:
        if not review_after_run_due(kw.get("regime", ""), kw.get("meta")):
            return
        key = str(kw.get("user_id") or "")
        if key in _REVIEWS_IN_FLIGHT:
            logger.info("after-run review of %s skipped: a review for this user is "
                        "still running", kw.get("rail_id", "?"))
            return
        from core.async_bridge import spawn_retained
        _REVIEWS_IN_FLIGHT.add(key)
        try:
            spawn_retained(_review_after_run_once(key, task_agent, **kw))
        except Exception:
            _REVIEWS_IN_FLIGHT.discard(key)
            raise
    except Exception as e:
        logger.debug("after-run review not scheduled: %s", e)
