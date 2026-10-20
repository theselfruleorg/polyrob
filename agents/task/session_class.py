"""Is this session AUTONOMOUS (a cron / goal / planner run) or interactive? — 057.

Principle 3 of 057: *autonomous ≠ interactive*. A scheduled or board-dispatched
run should get its own budget class — its own tool rig, context cap, output cap
and timeout — instead of inheriting the chat defaults. Everything in WS-A/WS-B
that needs to know "which kind of session am I in" asks HERE, so there is one
answer and not five heuristics (``skip_memory``, ``cron=True``,
``SessionProfile``, ``creator``, "does it have a goal_id").

The marker already exists and is already authoritative:
``core.autonomy_marker`` is stamped by
``agents/task/runtime/run_as_session.py`` **before** ``create_session``, so it is
visible during construction — when the tool catalog and limits are pinned.
Probe failures are treated as autonomous because this classification also gates
security-sensitive actions, not only budgets.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def is_autonomous_session(session_id: Optional[str]) -> bool:
    """True for a cron / goal / planner-spawned run. Fail-closed to True on probe errors."""
    if not session_id:
        return False
    try:
        from core.autonomy_marker import is_autonomous
        return bool(is_autonomous(str(session_id)))
    except Exception:  # pragma: no cover - defensive
        logger.debug("autonomy marker unreadable; treating session as autonomous",
                     exc_info=True)
        return True


def budget_class_autonomous(session_id: Optional[str]) -> bool:
    """The BUDGET reading of :func:`is_autonomous_session` (context, output,
    cache TTL, disclosure, timeout shape). Fail-OPEN to interactive: a probe error
    must not hand an owner chat the autonomous budgets. Security gates keep the
    fail-closed reading."""
    if not session_id:
        return False
    try:
        from core.autonomy_marker import is_autonomous
        return bool(is_autonomous(str(session_id)))
    except Exception:  # pragma: no cover - defensive
        logger.debug("autonomy marker unreadable; budget class interactive", exc_info=True)
        return False


def tool_disclosure_enabled(session_id: Optional[str] = None) -> bool:
    """Whether this session pins the ``<tool-catalog>`` block + ``load_tool``.

    ``TOOL_PROGRESSIVE_DISCLOSURE`` (the existing flag; ON under POLYROB_LOCAL)
    OR — 057 WS-A — ``AUTONOMOUS_TOOL_DISCLOSURE`` for an autonomous session on
    a server that never sets POLYROB_LOCAL. That second key is what makes a
    NARROW rig safe: a job that finds it needs one more tool can load it,
    instead of failing and filing an owner ask.

    The hard lines are unchanged and live in ``tools/tool_disclosure.py``: money
    tools are NEVER loadable, delegate-blocked ids stay blocked for leaves, and
    loading registers schemas only — it grants no execution right.

    An OWNER chat seat (:func:`stamp_owner_seat`, 081 WS-3.1) always pins it, with
    no flag: it runs on the narrow ``owner`` rig, and the catalog is its way to
    every other tool.
    """
    from core.config_policy import tool_progressive_disclosure
    if tool_progressive_disclosure():
        return True
    from core.autonomy_marker import is_owner_seat
    if is_owner_seat(session_id):
        return True
    from core.config_policy.capability_toggles import autonomous_tool_disclosure
    return autonomous_tool_disclosure() and budget_class_autonomous(session_id)


def stamp_owner_seat(orchestrator=None, *, session_id: Optional[str] = None,
                     user_id: Optional[str] = None, public: bool = False,
                     correspondent: bool = False) -> bool:
    """081 WS-3.1: mark a session as an OWNER chat seat when it is the OWNER_CHAT
    regime (not a room, not a correspondent session, not an autonomous run) AND
    its user is this instance's owner principal — a tenant's API session is the
    OWNER_CHAT regime too, and never a seat.

    Call it BEFORE the ``SessionOrchestrator`` is built (its Controller registers
    ``load_tool`` / ``tool_call`` in its constructor): pass the facts by keyword
    (creation and recreation), or an orchestrator-shaped object. Fail-closed to
    False: an unmarked session keeps the global disclosure and late-tool mode.
    """
    if orchestrator is None:
        from types import SimpleNamespace
        orchestrator = SimpleNamespace(session_id=session_id, user_id=user_id,
                                       _public_session=bool(public),
                                       _correspondent_session=bool(correspondent))
    try:
        sid = getattr(orchestrator, "session_id", None)
        if session_regime(orchestrator=orchestrator, session_id=sid) != OWNER_CHAT:
            return False
        from core.instance import resolve_owner_principal
        owner = resolve_owner_principal()
        user_id = getattr(orchestrator, "user_id", None)
        if not (owner and user_id and str(user_id) == str(owner)):
            return False
        from core.autonomy_marker import mark_owner_seat
        mark_owner_seat(sid)
        return True
    except Exception:  # pragma: no cover - defensive
        logger.debug("owner-seat stamp skipped", exc_info=True)
        return False


def apply_session_output_budget(llm, session_id: Optional[str]) -> int:
    """Stamp ``AUTONOMOUS_MAX_OUTPUT_TOKENS`` on *llm* for an autonomous session.

    The agents-tier half of 057 WS-B's output budget: ``modules/llm`` owns the
    stamping and the ceiling arithmetic, this owns the session-class decision
    (``modules`` may not import ``agents``). Returns the cap in force, 0 = none.
    """
    from modules.llm.output_budget import apply_output_budget, autonomous_max_output_tokens
    cap = autonomous_max_output_tokens()
    if cap <= 0 or not budget_class_autonomous(session_id):
        return 0
    return apply_output_budget(llm, cap)


def apply_session_cache_ttl(llm, session_id: Optional[str]) -> Optional[str]:
    """Stamp the Anthropic prompt-cache TTL for this session's class — F4.

    ``ANTHROPIC_CACHE_TTL=auto`` (the default) buys the **1h** cache window for
    an interactive turn and keeps the API-default **5m** one for an autonomous
    run. An owner's chat is bursty: the next turn lands minutes later, by which
    time a 5-minute cache has expired and the whole tools+system prefix is
    re-paid at full price. A cron/goal run is one burst that never returns, so
    the longer window would only pay the 2x cache-WRITE premium for nothing.

    The agents-tier half of the seam: ``modules/llm/cache_hints.py`` owns the
    stamp and the reader (``modules`` may not import ``agents``), this owns the
    session-class decision. Returns the ttl in force, or None.
    """
    from modules.llm.cache_hints import apply_cache_ttl, resolve_cache_ttl
    ttl = resolve_cache_ttl(budget_class_autonomous(session_id))
    return apply_cache_ttl(llm, ttl)


def correspondent_facing(orchestrator) -> bool:
    """True when a third party's text drives this session: it was CREATED for a
    correspondent (``_correspondent_session``, stamped at creation and restored
    from the session's ``creator`` on recreate — H16) or it is currently
    correspondent-TAINTED (``_correspondent_tainted``). Owner-only state (the
    owner thread, owner asks) never enters such a session.

    Fail-CLOSED on a probe error: this guards disclosure, not a budget.
    """
    if orchestrator is None:
        return False
    try:
        from core.security.taints import correspondent_tainted
        return bool(getattr(orchestrator, "_correspondent_session", False)
                    or correspondent_tainted(orchestrator))
    except Exception:  # pragma: no cover - defensive
        return True


def may_write_owner_memory(orchestrator) -> bool:
    """Third-party sessions must never promote findings into owner recall.

    A thin delegator over ``REGIME_POLICY[...].owner_memory`` (the ONE source;
    the 2026-10-15 review found the two disagreeing on CHILD). It sees only an
    orchestrator, so it cannot tell a CHILD — a writer that holds the agent
    asks ``regime_policy(agent)`` instead."""
    return regime_policy(orchestrator=orchestrator,
                         session_id=getattr(orchestrator, "session_id", None)).owner_memory


# ── Regimes (2026-10-13 memory/coherency review) ────────────────────────────
#
# Every coherency fix of that review behaves differently per KIND of session:
# an owner chat lives for days and its latest owner turn wins; a cron or goal
# run is one burst whose task IS the pinned instruction and whose own past runs
# are the most relevant memory; a room / correspondent session must never read
# or write owner state; a child gets a hand-off, not a conversation. Before
# this table each site re-derived that from ``_is_sub_agent``,
# ``correspondent_facing``, ``is_autonomous`` and ``goal_for_session`` in its
# own order. Ask ``session_regime`` for the kind, and ``regime_policy`` for the
# per-kind decision — add a column here, never a sixth heuristic at the site.

OWNER_CHAT = "owner_chat"
CRON = "cron"
GOAL = "goal"
AUTONOMOUS = "autonomous"      # planner / self-wake run: no job or goal row
ROOM = "room"
CORRESPONDENT = "correspondent"
CHILD = "child"                # a delegated sub-agent

REGIMES = (OWNER_CHAT, CRON, GOAL, AUTONOMOUS, ROOM, CORRESPONDENT, CHILD)


def session_regime(agent=None, *, orchestrator=None, session_id: Optional[str] = None) -> str:
    """The ONE answer to "what kind of session is this".

    Order matters and is security-first: a third-party-facing session is ROOM /
    CORRESPONDENT whatever else it is; a child is a CHILD even when its parent
    is a cron run. A probe error falls to the most restrictive regime the
    evidence allows (``correspondent_facing`` is already fail-closed).
    """
    if agent is not None:
        orchestrator = orchestrator if orchestrator is not None else getattr(agent, "orchestrator", None)
        session_id = session_id or getattr(agent, "session_id", None)
        if getattr(agent, "_is_sub_agent", False):
            return CHILD
    try:
        from core.surfaces.room_policy import is_public_session
        if orchestrator is not None and is_public_session(orchestrator):
            return ROOM
    except Exception:  # pragma: no cover - defensive
        logger.warning("room probe failed for session %s; treating it as a ROOM "
                       "(most restrictive)", session_id, exc_info=True)
        return ROOM
    if correspondent_facing(orchestrator):
        return CORRESPONDENT
    if not is_autonomous_session(session_id):
        return OWNER_CHAT
    try:
        from core.autonomy_marker import cron_job_for_session, goal_for_session
        if goal_for_session(session_id):
            return GOAL
        if cron_job_for_session(session_id):
            return CRON
    except Exception:  # pragma: no cover - defensive
        logger.debug("job/goal probe failed for session %s; regime AUTONOMOUS",
                     session_id, exc_info=True)
    return AUTONOMOUS


class RegimePolicy:
    """Per-regime decisions. Plain attributes, frozen by convention (a table row)."""

    __slots__ = ("pin_initial_task", "latest_owner_turn_wins", "demote_automation_recall",
                 "recall_own_runs_first", "owner_memory", "run_record",
                 "child_owner_brief", "model_tier")

    def __init__(self, *, pin_initial_task: bool, latest_owner_turn_wins: bool,
                 demote_automation_recall: bool, recall_own_runs_first: bool,
                 owner_memory: bool, run_record: bool, child_owner_brief: bool = False):
        #: the session's first message is THE instruction (cron/goal task, child goal);
        #: False = it is only "the first message of this chat", dated, and newer
        #: owner turns override it.
        self.pin_initial_task = pin_initial_task
        #: a later owner turn revises the task and survives compaction as an instruction.
        self.latest_owner_turn_wins = latest_owner_turn_wins
        #: recall ranks cron/goal rows below chat rows (owner chat: automation noise).
        self.demote_automation_recall = demote_automation_recall
        #: recall ranks rows of THIS job / goal first (its own continuity).
        self.recall_own_runs_first = recall_own_runs_first
        #: may read owner docs/recall and promote findings into owner memory.
        self.owner_memory = owner_memory
        #: writes / reads a per-job or per-goal run record on every exit path.
        self.run_record = run_record
        #: WS5 D4: a delegated child gets a short dated brief of this session's
        #: latest owner ask (third-party sessions and children: never).
        self.child_owner_brief = child_owner_brief
        #: the model tier (``core.runtime_config.MODEL_TIERS``); set below the table.
        self.model_tier = "default"


REGIME_POLICY = {
    OWNER_CHAT: RegimePolicy(pin_initial_task=False, latest_owner_turn_wins=True,
                             demote_automation_recall=True, recall_own_runs_first=False,
                             owner_memory=True, run_record=False),
    CRON: RegimePolicy(pin_initial_task=True, latest_owner_turn_wins=True,
                       demote_automation_recall=False, recall_own_runs_first=True,
                       owner_memory=True, run_record=True),
    GOAL: RegimePolicy(pin_initial_task=True, latest_owner_turn_wins=True,
                       demote_automation_recall=False, recall_own_runs_first=True,
                       owner_memory=True, run_record=True),
    AUTONOMOUS: RegimePolicy(pin_initial_task=True, latest_owner_turn_wins=True,
                             demote_automation_recall=False, recall_own_runs_first=False,
                             owner_memory=True, run_record=False),
    ROOM: RegimePolicy(pin_initial_task=False, latest_owner_turn_wins=False,
                       demote_automation_recall=True, recall_own_runs_first=False,
                       owner_memory=False, run_record=False),
    CORRESPONDENT: RegimePolicy(pin_initial_task=False, latest_owner_turn_wins=False,
                                demote_automation_recall=True, recall_own_runs_first=False,
                                owner_memory=False, run_record=False),
    CHILD: RegimePolicy(pin_initial_task=True, latest_owner_turn_wins=False,
                        demote_automation_recall=False, recall_own_runs_first=False,
                        owner_memory=False, run_record=False),
}


# WS5 D4 column (kept out of the row literals so parallel column additions merge).
for _regime in (OWNER_CHAT, CRON, GOAL, AUTONOMOUS):
    REGIME_POLICY[_regime].child_owner_brief = True
del _regime

# Model tier column (2026-10-14 review B-8 / E-9). ``default`` = the operator's model;
# a CRON / GOAL / AUTONOMOUS run is escalated to ``heavy`` (``HEAVY_MODEL``) by
# ``core.money.classify.is_money_class(payload)`` inside ``core.runtime_config.model_for``;
# a CHILD inherits its parent's LLM. Owner chat stays on ``default`` (a per-turn
# switch re-sends ~140k uncached; a money operation hands off to a money-class run).
for _regime in REGIMES:
    REGIME_POLICY[_regime].model_tier = "inherit" if _regime == CHILD else "default"
del _regime


def regime_model_for(regime: str, payload, provider, env=None):
    """The model for a run of *regime* — ``core.runtime_config.model_for`` at that
    regime's ``model_tier``. ``None`` = the caller fills the provider's default
    (or, for ``inherit``, keeps the parent's model)."""
    from core.runtime_config import model_for
    return model_for(payload, provider, tier=REGIME_POLICY[regime].model_tier, env=env)


def regime_policy(agent=None, *, orchestrator=None, session_id: Optional[str] = None) -> RegimePolicy:
    """``REGIME_POLICY[session_regime(...)]`` — the per-kind decisions for this session."""
    return REGIME_POLICY[session_regime(agent, orchestrator=orchestrator, session_id=session_id)]
