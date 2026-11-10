"""Post-turn background-review fork (W2-C, Reference-parity self-improvement loop).

After a run of productive turns, the agent forks a cheap, fire-and-forget sub-agent
that reviews what just happened and — when it finds a durable, reusable procedure —
authors a skill (via the W2 ``skill_manage`` tool; it is live at once).
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

Gated ``BACKGROUND_REVIEW_ENABLED`` (default OFF). What it writes is live at once, with
the prior body archived and an owner line in the recap; a correspondent or room session
never runs a reviewer. After a goal or cron run it runs only on a bad signal (108 W6:
``review_signal``), and the curator measures each live change (``lane_measure``).
"""
from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

_REVIEW_PROMPT = (
    "You are a background reviewer for an AI agent. The agent just finished a run of "
    "work in this session. Review the digest of its recent steps below (it is all "
    "you see of that work) and decide whether a "
    "DURABLE, REUSABLE procedure emerged that would help in future sessions.\n\n"
    "If yes: call skill_manage(action='create', skill_id=<short-kebab-id>, "
    "when=<one line of <= 60 chars: when to load it>, "
    "content=<a concise SKILL.md: a '# Title' heading then when-to-use + steps>, "
    "reason=<one sentence for the owner: what in this work the skill addresses>). "
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
            prev = getattr(self, "_bg_review_task", None)
            if prev is not None and not prev.done():
                return  # 098 AG-12: one inline reviewer per agent at a time
            if not self._bg_review_should_fire(turn_was_productive):
                return
            task = asyncio.create_task(self._run_background_review())
            self._bg_review_task = task
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
            from agents.task.agent.core.llm_slots import cached_judge_llm
            try:
                # P2-9: async provisioning — don't block the loop building the client.
                aux = await cached_judge_llm(self)
            except Exception:
                aux = None
            # P2-10: make the reviewer's model cost VISIBLE. On openrouter the cheap aux
            # model keys on the MAIN model's family (the spec's aux_family_map:
            # z-ai/glm-5* -> z-ai/glm-5.3-flash); a family with no row (deepseek,
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
            import time as _time
            _started = _time.time()
            await manager.run_subtask(
                task=build_review_prompt(digest),
                parent_agent_id=self.agent_id,
                profile_id="executor",
                max_steps=AutonomyConfig.bg_review_max_steps(),
                parent_llm=_review_llm,
                skip_complexity_check=True,
                exclude_actions=set(REVIEWER_EXCLUDED_ACTIONS),
            )
            _uid = getattr(self, "user_id", None) or getattr(
                getattr(self, "orchestrator", None), "user_id", None)
            _record_fallback_reasons(
                _uid, _drafts_since(_uid, _started - 1),
                "a review of the session's recent work found a step worth keeping")
        except Exception as e:
            logger.debug("background-review run failed (ignored): %s", e)


# --------------------------------------------------------------------------- #
# 095 W13: the reviewer AFTER a goal / cron run, on a trusted outcome signal.
# --------------------------------------------------------------------------- #

_RUN_REVIEW_PROMPT = (
    "You are a background reviewer for an AI agent. One of its scheduled runs just ended "
    "with a bad signal: the owner corrected it, its check failed, or it stopped on an "
    "error. You see the run's outcome record, the run's skills and a digest of its last "
    "steps; that is all you see of that work.\n\n"
    "NO CHANGE IS THE DEFAULT. Most runs teach nothing durable: call done() with a "
    "one-line reason.\n"
    "A fault in a tool, the harness, a provider or the setup is a mechanism fault. No "
    "skill can fix it, so change no skill: call done(text='MECHANISM: <one line for the "
    "developer: what broke and where>').\n"
    "Change a skill only when the signal shows a wrong or missing step that a skill "
    "teaches (a failed run: the step that would have prevented the failure). Write at "
    "most ONE change, in this order of preference:\n"
    "(a) PATCH a skill in LANE SKILLS below, in ONE call: skill_manage(action='patch', "
    "skill_id=<id>, expected_sha256=<its sha256 below>, old_string=<the smallest span, "
    "copied exactly from its body>, new_string=<that span plus one bullet>, "
    "reason=<one sentence>). old_string, new_string and reason all go in that one call; "
    "a call with reason alone is refused.\n"
    "(b) If no loaded skill fits, patch the same way the first pinned skill of the "
    "run, with one lesson line.\n"
    "(c) Only if neither fits: skill_manage(action='create', skill_id=<short-kebab-id>, "
    "when=<one line of <= 60 chars>, content=<a '# Title' heading, when to use, steps>, "
    "reason=<one sentence>): search the skill index first and never duplicate a skill.\n"
    "reason is for the owner: what the signal showed in this run, and what this change "
    "addresses. The system adds the run and the signal to it.\n"
    "Keep it general: no secrets, no ids, no amounts, no dates of this run. Every "
    "write is live for the next run. Write skills only: do not edit owner or "
    "self documents, do not message anyone, do not do the run's work again."
)

#: The lane skills a review prompt shows, and the part of each body it shows.
_LANE_SKILLS_MAX = 4
_LANE_BODY_MAX_CHARS = 8000
_MECHANISM_RE = re.compile(r"^\s*MECHANISM:\s*(.+)$", re.M)
_MECHANISM_MAX_CHARS = 200


def _classified_failure(stop_reason) -> bool:
    """A typed stop reason that names a failure (not done / a pause / an owner stop)."""
    from agents.task.runtime.stop_reason import QUIET_STOPS, StopReason
    value = str(stop_reason or "")
    return value in {r.value for r in StopReason} and value not in QUIET_STOPS


def run_error(meta, outcome="") -> bool:
    """Did this run stop on an error? An owner stop or a pause is not an error."""
    from agents.task.runtime.stop_reason import QUIET_STOPS, StopReason
    m = meta if isinstance(meta, dict) else {}
    stop = str(m.get("stop_reason") or "")
    if stop in QUIET_STOPS and stop != StopReason.DONE.value:
        return False
    return (str(outcome or "") == "failed" or _classified_failure(stop)
            or bool(str(m.get("error") or "").strip()))


def review_signal(meta, outcome="") -> str:
    """The bad signal this run carries: ``owner_correct``, ``verified_failed`` or
    ``run_error``; ``""`` when it has none. R1: each one comes from the owner or from code,
    never from the agent's own report (108 W6)."""
    from agents.task.agent.core.episodic_digest import verified_state
    m = meta if isinstance(meta, dict) else {}
    if str(m.get("owner_verdict") or "").strip() == "correct":
        return "owner_correct"
    if verified_state(m.get("verified")) is False:
        return "verified_failed"
    return "run_error" if run_error(m, outcome) else ""


def review_after_run_due(regime: str, meta, outcome="") -> bool:
    """Pure: does this run record earn a review after the run? The regime must have
    ``RegimePolicy.review_after_run``, and the record must carry a bad signal
    (:func:`review_signal`). A plain success gets no review. Never raises."""
    try:
        from agents.task.session_class import REGIME_POLICY
        policy = REGIME_POLICY.get(regime)
        if policy is None or not policy.review_after_run:
            return False
        return bool(review_signal(meta, outcome))
    except Exception:
        logger.debug("review_after_run_due: probe failed", exc_info=True)
    return False


def _run_failed(outcome, meta: dict) -> bool:
    """A run whose own account is not a clean finish: its lessons stay drafts."""
    return str(outcome or "") != "done" or _classified_failure(meta.get("stop_reason"))


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
            f"owner_verdict={_v('owner_verdict')} stop={_v('stop_reason')} "
            f"signal={_v('signal_source')} skills={skills}")


def lane_skill_blocks(user_id, skill_ids, created_by: str) -> str:
    """The body and sha256 of each skill of the lane, read from the file the write lands in,
    so a patch needs no load step and its ``expected_sha256`` is right. Prod: the old flow
    spent steps on it and 22 of 68 reviews hit the step limit. Fail-open per skill."""
    from agents.task.agent.skill_manager import get_skill_manager
    blocks = []
    try:
        sm = get_skill_manager()
    except Exception:
        logger.debug("lane skills unreadable", exc_info=True)
        return ""
    for sid in list(dict.fromkeys(str(s) for s in skill_ids or [] if str(s or "").strip()))[:_LANE_SKILLS_MAX]:
        try:
            base = sm._edit_base(sid, str(user_id or ""), created_by, None, None)
            if not isinstance(base, tuple):
                continue
            body, sha = base[1], base[2]
            clipped = len(body) > _LANE_BODY_MAX_CHARS
            note = "\n(the body is cut here: load_skill shows the rest)" if clipped else ""
            blocks.append(f'<skill id="{sid}" sha256="{sha}">\n{body[:_LANE_BODY_MAX_CHARS]}{note}\n</skill>')
        except Exception:
            logger.debug("lane skill %s unreadable", sid, exc_info=True)
    return "\n".join(blocks)


def build_run_review_prompt(digest: str, *, outcome_line: str, pinned: list = None,
                            lane_skills: str = "") -> str:
    """The after-run reviewer task: the outcome line, the loaded skills with their
    patch generation, the run's first pinned skill, the body and sha256 of each lane skill,
    and the step digest (DATA)."""
    head = _RUN_REVIEW_PROMPT + "\n\nRUN OUTCOME: " + outcome_line
    pins = [str(p) for p in (pinned or []) if str(p).strip()]
    head += "\nFIRST PINNED SKILL OF THIS RUN: " + (pins[0] if pins else "(none)")
    head += "\nLANE SKILLS:\n" + (lane_skills or "(none readable: load_skill(<id>) gives a body "
                                                 "and its sha256)")
    return build_review_prompt(digest, base=head)


def _mechanism_line(result) -> str:
    """The note in the reviewer's done() text; ``""`` when it has none."""
    output = getattr(result, "output", "")
    text = str(getattr(output, "final_text", output) or "")
    match = _MECHANISM_RE.search(text)
    return " ".join(match.group(1).split())[:_MECHANISM_MAX_CHARS] if match else ""


def _record_mechanism_note(line: str, *, user_id, session_id, rail_id) -> None:
    """A mechanism fault goes to the digest for the developer, not into a skill. Fail-open."""
    try:
        from core.event_kinds import REVIEW_MECHANISM_NOTE
        from core.event_log import event_log_enabled, get_event_log
        if event_log_enabled():
            get_event_log().record(REVIEW_MECHANISM_NOTE, user_id=str(user_id or ""),
                                   session_id=str(session_id or ""), source="background_review",
                                   attrs={"note": line, "rail": rail_id})
    except Exception:
        logger.debug("reviewer note not recorded", exc_info=True)


def _main_agent(orchestrator):
    for agent in list((getattr(orchestrator, "agents", None) or {}).values()):
        if not getattr(agent, "_is_sub_agent", False):
            return agent
    return None


def _run_reason(rail_id, regime, outcome, meta) -> str:
    """096 W1: the owner-readable fallback reason for an after-run draft."""
    m = meta if isinstance(meta, dict) else {}
    bits = [f"outcome: {outcome or 'unknown'}"]
    if m.get("stop_reason"):
        bits.append(f"stopped: {m['stop_reason']}")
    return (f"learned from the {regime or 'scheduled'} run {rail_id or '(unnamed)'} "
            f"({', '.join(bits)})")


def _record_fallback_reasons(user_id, drafts: list, reason: str) -> None:
    """096 W1: a reviewer draft is never left without a reason — when the model
    gave none, record the run it learned from. Never raises."""
    if not drafts:
        return
    try:
        from core import pending_reasons
        from core.instance import resolve_instance_id
        home = _prefs_home_dir()
        for d in drafts:
            item_id = d.split(" ", 1)[-1]
            pending_reasons.record(str(user_id or ""), "skill", item_id, reason,
                                   by="background_review", home_dir=home,
                                   instance_id=resolve_instance_id(), only_if_missing=True)
    except Exception:
        logger.debug("reviewer fallback reason not recorded", exc_info=True)


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
                    and not a.get("pending") and a.get("action") in ("create", "patch", "edit")):
                item = f"{a.get('action')} {a.get('item_id')}"
                if item not in out:
                    out.append(item)
        return out
    except Exception:
        logger.debug("reviewer drafts unreadable", exc_info=True)
        return []


async def _run_reviewer(agent, manager, *, regime, session_id, user_id, rail_id, outcome, meta,
                        pinned, digest: str) -> None:
    """One review. The run has ended, so this spends nothing of its step budget: the child
    gets ``BG_REVIEW_MAX_STEPS`` on the aux (judge) model, and its usage rolls up to *session_id*.
    Each live change writes one ``self_modification`` row, and its measure row (108 W6)."""
    import time
    from core.config_policy import AutonomyConfig
    from modules.skills.writer import PROVENANCE_BACKGROUND, PROVENANCE_UNTRUSTED, REVIEW_SCOPE, REVIEWING_FAILED_RUN
    from agents.task.agent.core.llm_slots import cached_judge_llm
    try:
        aux = await cached_judge_llm(agent)  # closed once at session cleanup (M2)
    except Exception:
        aux = None
    m = meta if isinstance(meta, dict) else {}
    failed = _run_failed(outcome, m)
    signal = review_signal(m, outcome)
    lane_skills = lane_skill_blocks(user_id, list(pinned or []) + list(m.get("skills_loaded") or []),
                                    PROVENANCE_UNTRUSTED if failed else PROVENANCE_BACKGROUND)
    started = time.time()
    failed_token = REVIEWING_FAILED_RUN.set(failed)
    scope_token = REVIEW_SCOPE.set(f"run={rail_id} signal={signal}")
    try:
        result = await manager.run_subtask(
            task=build_run_review_prompt(digest, outcome_line=run_outcome_line(outcome, m),
                                         pinned=pinned, lane_skills=lane_skills),
            parent_agent_id=agent.agent_id,
            profile_id="executor",
            max_steps=AutonomyConfig.bg_review_max_steps(),
            parent_llm=aux or agent.llm,
            skip_complexity_check=True,
            exclude_actions=set(REVIEWER_EXCLUDED_ACTIONS),
        )
    finally:
        REVIEW_SCOPE.reset(scope_token)
        REVIEWING_FAILED_RUN.reset(failed_token)
    note = _mechanism_line(result)
    if note:
        _record_mechanism_note(note, user_id=user_id, session_id=session_id, rail_id=rail_id)
    drafts = _drafts_since(user_id, started - 1)
    _record_fallback_reasons(user_id, drafts, _run_reason(rail_id, regime, outcome, m))
    from agents.task.agent.core.lane_measure import record_changes
    from modules.skills.skill_usage import get_skill_usage_store
    measured = await record_changes(get_skill_usage_store(), user_id=str(user_id or ""),
                                    since_ts=started - 1, rail_id=rail_id, signal=signal, meta=m)
    logger.info("🪞 after-run review of %s done (signal %s, %d draft(s), %d measure row(s))",
                rail_id, signal, len(drafts), measured)


async def review_after_run(task_agent, *, regime: str, session_id, user_id,
                           rail_id: str, outcome: str, meta, pinned=None) -> bool:
    """Run the reviewer on a finished run's resident session. True when a reviewer ran.
    Fail-open; never raises."""
    try:
        if not session_id or not review_after_run_due(regime, meta, outcome):
            return False
        if not _review_gates_open(user_id):
            return False
        orch = task_agent.get_orchestrator(session_id)
        agent = _main_agent(orch)
        manager = orch.get_sub_agent_manager() if orch is not None else None
        if agent is None or manager is None:
            return False
        try:
            digest = build_review_digest(getattr(getattr(agent, "history", None),
                                                 "history", None))
        except Exception:
            digest = ""
        await _run_reviewer(agent, manager, regime=regime, session_id=session_id, user_id=user_id,
                            rail_id=rail_id, outcome=outcome, meta=meta, pinned=pinned, digest=digest)
        return True
    except Exception as e:
        logger.debug("after-run review skipped (%s): %s", rail_id, e)
        return False


def _episode_digest(episode, meta: dict) -> str:
    """The run as its record tells it: the owner's correction often comes after the run's
    session has gone, so its steps are not there to read."""
    parts = [f"task: {_clip(getattr(episode, 'task', ''))}",
             f"summary: {_clip(getattr(episode, 'summary', ''), _DIGEST_MAX_CHARS // 2)}",
             f"the owner's correction: {_clip(meta.get('owner_verdict_quote'))}"]
    return "\n".join(parts)


async def review_after_verdict(agent, orchestrator, *, episode, rail_id: str, meta) -> bool:
    """Run the reviewer on the owner's correction of a run, from the session that took the
    reply. True when a reviewer ran. Fail-open; never raises."""
    try:
        regime, user_id = getattr(episode, "kind", ""), getattr(episode, "user_id", "")
        outcome = getattr(episode, "outcome", "") or ""
        if not review_after_run_due(regime, meta, outcome) or not _review_gates_open(user_id):
            return False
        manager = orchestrator.get_sub_agent_manager() if orchestrator is not None else None
        if agent is None or manager is None:
            return False
        from agents.task.agent.core.episodic_digest import rail_payload
        from core.config_policy.rigs import pinned_skills
        await _run_reviewer(agent, manager, regime=regime, session_id=getattr(episode, "session_id", ""),
                            user_id=user_id, rail_id=rail_id, outcome=outcome, meta=meta,
                            pinned=pinned_skills(rail_payload(rail_id)),
                            digest=_episode_digest(episode, meta))
        return True
    except Exception as e:
        logger.debug("review after verdict skipped (%s): %s", rail_id, e)
        return False


#: Users with an after-run reviewer in flight. A burst of runs spawns at most ONE
#: reviewer per user at a time (the rest are skipped and logged); the sub-agent
#: semaphore (MAX_CONCURRENT_SUB_AGENTS) still bounds the process.
_REVIEWS_IN_FLIGHT: set = set()


#: Owner corrections that arrived while that user's reviewer ran: each one earns its review
#: (the verdict marks a run corrected only once), so it waits instead of being skipped.
_QUEUED_REVIEWS: dict = {}
_QUEUED_MAX = 5


async def _once(key: str, make_coro) -> bool:
    try:
        return await make_coro()
    finally:
        _REVIEWS_IN_FLIGHT.discard(key)


def _release(key: str) -> None:
    """The user's reviewer ended: free the slot and start the next queued review."""
    _REVIEWS_IN_FLIGHT.discard(key)
    queued = _QUEUED_REVIEWS.get(key)
    if not queued:
        return
    make_coro, rail_id = queued.pop(0)
    if not queued:
        _QUEUED_REVIEWS.pop(key, None)
    try:
        _spawn_one_per_user(key, make_coro, rail_id, queue=True)
    except Exception as e:
        logger.warning("queued review of %s not started: %s", rail_id, e)


def _spawn_one_per_user(key: str, make_coro, rail_id, queue: bool = False) -> None:
    """Start the review unless this user has one running. Then a ``queue`` review waits
    for it (bounded), and any other is skipped. Never raises."""
    if key in _REVIEWS_IN_FLIGHT:
        waiting = _QUEUED_REVIEWS.setdefault(key, []) if queue else None
        if waiting is not None and len(waiting) < _QUEUED_MAX:
            waiting.append((make_coro, rail_id))
            logger.info("review of %s waits: a review for this user is still running", rail_id)
            return
        logger.info("after-run review of %s skipped: a review for this user is "
                    "still running", rail_id)
        return
    from core.async_bridge import spawn_retained
    _REVIEWS_IN_FLIGHT.add(key)
    try:
        task = spawn_retained(_once(key, make_coro))
    except Exception:
        _REVIEWS_IN_FLIGHT.discard(key)
        raise
    # 098 AG-12: the release rides on the TASK's end — a task cancelled
    # before its first step never runs the coroutine's finally.
    task.add_done_callback(lambda _t, _k=key: _release(_k))


def schedule_review_after_run(task_agent, **kw) -> None:
    """Fire-and-forget :func:`review_after_run` (never inside the run's budget or
    its scheduler's time cap). A cheap pure gate first, so a run that does not
    qualify spawns nothing; a user whose reviewer is still running is skipped.
    Never raises."""
    try:
        if not review_after_run_due(kw.get("regime", ""), kw.get("meta"), kw.get("outcome", "")):
            return
        _spawn_one_per_user(str(kw.get("user_id") or ""),
                            lambda: review_after_run(task_agent, **kw), kw.get("rail_id", "?"))
    except Exception as e:
        logger.debug("after-run review not scheduled: %s", e)


def schedule_review_after_verdict(agent, orchestrator, *, episode, rail_id: str, meta) -> None:
    """Fire-and-forget :func:`review_after_verdict`: one owner correction, one review.
    Never raises."""
    try:
        _spawn_one_per_user(str(getattr(episode, "user_id", "") or ""),
                            lambda: review_after_verdict(agent, orchestrator, episode=episode,
                                                         rail_id=rail_id, meta=meta), rail_id,
                            queue=True)
    except Exception as e:
        logger.debug("review after verdict not scheduled: %s", e)
