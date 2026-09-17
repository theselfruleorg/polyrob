"""`goal` agent tool (W4 — durable goal board).

Lets an agent record durable, cross-session goals it (or a dispatcher) will pursue
beyond the current turn — distinct from the session-scoped TODO (`task` tool) and
from `cronjob` (time-triggered). Thin glue over :class:`agents.task.goals.board.GoalBoard`;
all state/claim/breaker logic lives there.

Off by default: registered only when ``GOALS_ENABLED=true`` and never in the default
``tool_ids`` list, so production is unaffected until goals are turned on. Uses
``BaseTool.action(param_model=...)`` like ``cronjob_tools`` — NOT bare
``@registry.action`` closures — so ``from __future__ import annotations`` is safe here
(the param model is explicit, not introspected from a stringized annotation).
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from tools.base_tool import BaseTool
from tools.controller.types import ActionResult
from agents.task.goals.board import DuplicateGoalError, GoalBoard

logger = logging.getLogger(__name__)

# Proposal 001 (owner-approved option 2, 2026-07-01): tools an agent may request for its OWN
# self-created goals, filtered to a safe allowlist so it can self-direct research/content/coding
# (and — per owner — posting) but NOT self-grant money-SPEND (wallet/x402_pay/hyperliquid/
# polymarket), code execution, cron, or meta goal/skill tools.
# Proposal 009 (owner battle-test kickoff, 2026-07-13/14): the kickoff mission explicitly
# sanctions email outreach, telegram group/channel posting (`message` — every send is still
# gated by the owner outbound allowlist) and x402 INVOICING (receivables only, capped;
# x402_pay/spend stays excluded), plus knowledge notes.
# Proposal 029 R5 (2026-08-24): `defi_data` joins the allowlist. It is READ-only
# token sight — it constructs no signer and broadcasts nothing, and it is not in
# the capability table's `money` set — so excluding it was a tax on
# reconnaissance rather than a safety property: every screening goal had to wait
# for an operator-seeded cycle. `defi_trade` stays excluded, and that is exactly
# where the line belongs. (`portfolio` remains separately gated by NAME while
# correspondent-tainted; adding the tool here does not touch that.)
_SELF_GOAL_ALLOWED_TOOLS = frozenset(
    {"filesystem", "task", "browser", "perplexity", "mcp", "anysite", "coding", "web_fetch",
     "twitter", "email", "message", "x402_invoice", "knowledge", "defi_data"}
)


def allowed_self_goal_tools() -> frozenset:
    """Tool ids an agent-created goal may request. Under effective autonomous mode
    (AUTONOMY_MODE=autonomous on a single-owner instance) this expands to the full
    AUTONOMOUS_MODE_TOOLS grant; money-spend/host tools are in NEITHER set."""
    try:
        from agents.task.constants import autonomous_mode_tools
        from core.config_policy import full_autonomy_enabled
        if full_autonomy_enabled():
            # Folds in the defi rail when DEFI_AGENT_AUTONOMY is armed. ⚠️ That
            # DOES mean a goal the agent writes itself can carry `defi_trade`
            # once armed — deliberate, on the owner's 2026-09-12 directive. What
            # bounds an injected goal is the CAP and the per-verb simulation, not
            # the toolset: the per-tx ceiling, the rolling daily cap, the
            # kill-switch, the 031 pause and the owner queue above
            # DEFI_AUTONOMOUS_MAX_USD all still apply. Unarmed, this is the
            # historical set exactly.
            return frozenset(_SELF_GOAL_ALLOWED_TOOLS | set(autonomous_mode_tools()))
    except Exception:
        pass
    return _SELF_GOAL_ALLOWED_TOOLS


# Proposal 009 #1 (2026-07-14): whenever a self-created goal carries ANY tools payload, union in
# this safe baseline so the dispatched session is never starved of basics (night-1 failure mode:
# tools=['twitter'] sessions had no filesystem; text-only goals had no web_fetch to research).
_SELF_GOAL_BASELINE_TOOLS = ("filesystem", "task", "web_fetch", "knowledge")

# Proposal 009 option B: when the agent sets no `tools`, infer them from the goal's own text —
# the night-1 blocked goals literally named their needed tool in title/acceptance ("Publish
# queued OSS launch X thread" → twitter) but dispatched tool-starved. Substring match on
# lowercased title+body+acceptance; result is still filtered through _SELF_GOAL_ALLOWED_TOOLS,
# so inference can never grant more than an explicit request could.
_TOOL_TEXT_TOKENS = {
    "twitter": ("twitter", "tweet", "x thread", "x post", "x.com"),
    "email": ("email", "e-mail", "mailbox", "imap", "smtp"),
    "message": ("telegram", "t.me/", "channel"),
    "x402_invoice": ("x402", "invoice", "invoicing"),
    "web_fetch": ("web_fetch", "fetch", "http", "url", "website", "research", "browse"),
    "knowledge": ("knowledge", "notes"),
    "anysite": ("anysite",),
    "coding": ("coding",),
}


def _infer_tools_from_text(*texts: Optional[str]) -> set:
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return set()
    found = {tool for tool, tokens in _TOOL_TEXT_TOKENS.items()
             if any(tok in blob for tok in tokens)}
    return found & allowed_self_goal_tools()


# 2026-08-18 (intel finding, MEDIUM-HIGH — recurred across x402/video/micro-app goal
# families): self-created goals that EXPLICITLY narrow `tools` and carry an `http_ok`
# acceptance check are structurally doomed — dispatcher._resolve_goal_tools returns
# `payload.tools` VERBATIM the moment it's non-empty (agents/task/goals/dispatcher.py,
# "if own: return own"), with NO widening for compute posture, and shell/process/
# code_execution can NEVER be added via goal_create in the first place (deliberately
# excluded from both _SELF_GOAL_ALLOWED_TOOLS and AUTONOMOUS_MODE_TOOLS — a security
# boundary: host/compute tools ride AGENT_COMPUTE_POSTURE only, never a self-grant).
# So there is nothing to auto-append here; the only tools that could serve an http_ok
# check are the ones this function is forbidden from ever adding. The fix is to warn
# loudly at create time instead of letting the goal dispatch, burn its step budget
# trying to work around a tool it can never have, and die the identical way as its
# 3+ predecessors in this exact family (evidenced live: rob-status/video-render goals).
_COMPUTE_TOOL_IDS = frozenset({"shell", "process", "code_execution"})


def _dropped_tools_note(requested: Optional[List[str]]) -> str:
    """What the caller asked for and did NOT get, said out loud.

    The filter itself is correct and unchanged — an agent-created goal must
    never be able to acquire a money-SPEND verb, because the turn-origin gate
    treats a genuine autonomous goal turn as allowed, which makes this allowlist
    the only line stopping an INJECTED goal from trading.

    What was wrong is that the strip was SILENT. Dropped ids went to a log line
    the agent never sees, so `goal_create` returned success and the caller
    learned nothing; it then hit the gap at dispatch, filed "defi_trade not
    granted" as an owner ask, and repeated — roughly fifty times across two
    weeks of prod. A boundary the caller cannot see is one it cannot respect,
    and the money case needs the stronger sentence: not "ask again", but "this
    can never come from here".
    """
    if not requested:
        return ""
    allowed = allowed_self_goal_tools()
    dropped = [t for t in requested if t not in allowed]
    if not dropped:
        return ""
    from core.tool_capabilities import ids_with
    money = sorted(set(dropped) & set(ids_with("money")))
    note = ("\n⚠️ NOT granted (a goal you create yourself cannot carry these): "
            f"{', '.join(dropped)}.")
    if money:
        note += (f" {', '.join(money)} " + ("is a money tool" if len(money) == 1
                                            else "are money tools")
                 + " and can NEVER be granted this way — do not retry with "
                 "different wording. Only the owner/operator grants one: on a "
                 "goal THEY seeded, or at session creation. If you have a "
                 "candidate and no grant, write it where the granted run will "
                 "read it, and escalate ONCE rather than every run.")
    return note


def _compute_tool_mismatch_warning(tools: Optional[List[str]],
                                    acceptance_checks: Optional[List[Any]]) -> Optional[str]:
    """None if no mismatch; else a warning string to surface in the create result.

    Only fires when `tools` was explicitly set (narrowing away from the wide
    default) AND an `http_ok` check is present AND none of the compute tools
    are already in the list — i.e. exactly the combination that can never
    succeed, not a mere heuristic guess.
    """
    if not tools:
        return None
    if set(tools) & _COMPUTE_TOOL_IDS:
        return None
    has_http_ok = any(
        isinstance(c, dict) and c.get("type") == "http_ok" for c in (acceptance_checks or [])
    )
    if not has_http_ok:
        return None
    return (
        "⚠️ tool/acceptance mismatch: this goal's acceptance_checks include an "
        "http_ok probe (needs a live HTTP server), but self-created goals can NEVER "
        "be granted shell/process/code_execution — those are host/compute tools "
        "gated separately by AGENT_COMPUTE_POSTURE, not by this tool's `tools` "
        "param. With an explicit tools list set, dispatch uses it VERBATIM with no "
        "widening, so this goal will dispatch without compute tools and cannot "
        "serve HTTP. Recreate this goal WITHOUT the `tools` param (omit it "
        "entirely) so dispatch applies the wide autonomous default, which includes "
        "compute tools at the current posture."
    )


class GoalCreateAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., description="Short title for the durable goal.", min_length=3)
    body: str = Field("", description="Full instructions for pursuing the goal (what 'done' looks like).")
    priority: int = Field(5, ge=1, le=10, description="1-10, higher runs first.")
    tools: Optional[List[str]] = Field(
        None,
        description=("ALWAYS list every tool this goal needs to actually finish (e.g. 'twitter' "
                     "to post to X, 'email' to send mail, 'message' to post to telegram, "
                     "'x402_invoice' to invoice, 'web_fetch' to read the web) — a goal without "
                     "the right tools dispatches tool-starved and blocks. Filtered to a safe "
                     "allowlist; money-spend/code-exec/cron are never granted. If unset, tools "
                     "are inferred from the goal text and a safe baseline is applied."),
    )
    objective_id: Optional[str] = Field(None, description="Parent objective this goal advances.")
    depends_on: Optional[List[str]] = Field(
        None,
        description="Goal ids that must complete first — the goal waits until they are done.",
    )
    acceptance: Optional[str] = Field(None, description="What 'done' must prove (ids/paths/urls).")
    acceptance_checks: Optional[List[Any]] = Field(
        None,
        description=("Optional TYPED checks the framework executes at run end (fail-closed "
                     "when present). ONLY these types exist — do NOT invent others: "
                     "[{'type':'artifact_glob','pattern':'*.md'}, "
                     "{'type':'http_ok','url':'https://…'}, "
                     "{'type':'artifact','name':'report.md','contains':['A']}, "
                     "{'type':'file_contains','path':'report.md','contains':['A','B'],"
                     "'mode':'all'}]. Prefer setting one when the outcome is mechanically "
                     "checkable. At most 10 checks; malformed checks are rejected, never dropped. "
                     "Paths are run-workspace-relative; HTTP probes require public endpoints. "
                     "file_contains is a case-insensitive literal-substring match — use it "
                     "only for known-exact strings (an id, a file path, a specific number). "
                     "Do NOT use it to assert a report 'discusses'/'covers' a topic (e.g. "
                     "contains=['PnL'] to check the report mentions profit/loss) — a "
                     "semantically-complete report using different wording will fail this "
                     "check and force a wasted retry. For that kind of completeness, describe "
                     "it in plain-English `acceptance` instead and let the completion judge "
                     "read it."),
    )


class GoalListAction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = Field(
        None,
        description="Filter: triage/waiting/ready/running/blocked/done/cancelled. Omitted = "
                    "LIVE goals only (triage/waiting/ready/running/blocked); pass 'done' or "
                    "'cancelled' to see history.")
    limit: int = Field(30, ge=1, le=200, description="Newest N rows to show (default 30).")


#: What ``goal_list`` shows when no status filter is given: work that is still
#: on the board. ``done``/``cancelled`` are history and dwarf the live rows
#: within days (357 done vs 1 ready on prod, 2026-08-29).
from core.goal_vocab import LIVE_STATUS_ORDER as GOAL_LIST_LIVE_STATUSES  # noqa: E402


class GoalShowAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(..., description="The goal id.")


class GoalCancelAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(..., description="The goal id to cancel.")


class GoalUnblockAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(..., description="The blocked goal id.")
    rationale: str = Field("", description="Why it can proceed now (what changed).")


class ObjectiveAddAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., min_length=3, description="The standing objective (abstract or concrete).")
    body: str = Field("", description="Detail: what success looks like, constraints, cadence.")
    priority: int = Field(5, ge=1, le=10)
    success_criteria: str = Field(
        "", description="Measurable definition of done for this objective (e.g. metric + "
        "target + horizon). Surfaced to the planner so work is measured against what the "
        "owner wants, not a self-set proxy.")


class ObjectiveListAction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = Field(None, description="Filter: active/paused/done/dropped.")


class ObjectiveSetStatusAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective_id: str
    status: str = Field(..., description="pause | activate | drop")


class GoalUpdateAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str
    title: Optional[str] = Field(None, min_length=3)
    body: Optional[str] = None
    priority: Optional[int] = Field(None, ge=1, le=10)
    acceptance: Optional[str] = Field(None, description="What 'done' must prove (ids/paths/urls).")
    tools: Optional[List[str]] = None


_OBJ_STATUS_MAP = {"pause": "paused", "activate": "active", "drop": "dropped"}


def _autonomy_refusal(execution_context) -> Optional[ActionResult]:
    from agents.task.goals.autonomy_marker import is_autonomous
    sid = getattr(execution_context, "session_id", None)
    psid = getattr(execution_context, "parent_session_id", None)
    if is_autonomous(sid) or is_autonomous(psid):
        return ActionResult(
            error=("Refused: objective/goal mutation is not allowed from an autonomous "
                   "(goal/cron-spawned) session — owner-only. You may goal_create new "
                   "goals or read state."),
            include_in_memory=True)
    return None


class GoalTool(BaseTool):
    """Agent tool exposing create/list/show/cancel over the durable goal board."""

    def _resolve_board(self) -> GoalBoard:
        if getattr(self, "_goal_board", None) is None:
            from core.runtime_paths import goals_db_path
            data_dir = getattr(self.config, "data_dir", None) if getattr(self, "config", None) else None
            self._goal_board = GoalBoard(goals_db_path(data_dir))
        return self._goal_board

    @staticmethod
    def _user(execution_context) -> str:
        uid = getattr(execution_context, "user_id", None)
        if uid:
            return uid
        from core.identity import resolve_identity
        return resolve_identity()  # owner principal or "local" — never the anon sentinel (ME-D4)

    @staticmethod
    def _creating_turn_is_forged(execution_context) -> bool:
        """True unless this is a genuine owner turn (fail-closed on any probe error)."""
        try:
            from tools.controller.turn_origin import _is_forged_or_autonomous_turn
            return bool(_is_forged_or_autonomous_turn(execution_context, None))
        except Exception:
            return True

    @BaseTool.action("Record a DURABLE goal pursued across sessions (beyond this turn). "
                     "Use for ongoing objectives; use `task` for this-turn TODOs and `cronjob` "
                     "for time-scheduled runs.", param_model=GoalCreateAction)
    async def goal_create(self, params: GoalCreateAction, execution_context=None) -> ActionResult:
        user_id = self._user(execution_context)
        payload: dict = {}
        # 031: the AUDIT stamp — who created this goal — set on EVERY turn kind,
        # so the planner's own outcome accounting can count the goals it queued.
        # Distinct from origin_session_id (below), which is the WAKE target and
        # is stamped only for a genuine owner turn.
        _sid = getattr(execution_context, "session_id", None)
        if _sid:
            payload["created_by_session_id"] = str(_sid)
        # The creating session is the only session with a stake in this goal's
        # completion; the dispatcher's self-wake re-entry targets it (and ONLY it —
        # see GoalDispatcher._self_wake). Only a GENUINE turn (the owner's chat
        # session) is stamped: a planner/goal/cron session or a forged re-entry is
        # itself finished by the time the goal completes, and waking it would just
        # be the completion echo again — a planner session woken this way could
        # even queue more goals. Fail-closed: an undecidable origin stamps nothing.
        _origin = getattr(execution_context, "session_id", None)
        if _origin and not self._creating_turn_is_forged(execution_context):
            payload["origin_session_id"] = str(_origin)
        allowed: List[str] = []
        if params.tools:
            _allowed_set = allowed_self_goal_tools()
            allowed = [t for t in params.tools if t in _allowed_set]
            dropped = [t for t in params.tools if t not in _allowed_set]
            if dropped:
                logger.info("goal_create: dropped non-allowlisted tools %s (kept %s)", dropped, allowed)
        inferred: set = set()
        if not allowed:
            # S4 (dynamic tool rig, 2026-07-20): under progressive tool disclosure an
            # inference-only goal stays TOOLS-LESS — a written payload.tools would
            # short-circuit dispatch's wide autonomous default (the create-time
            # keyword guess was the last narrowing site), and the S1 catalog +
            # load_tool cover anything the guess would have added. Dispatch-time
            # inference (dispatcher._resolve_goal_tools) remains as a WIDENING hint.
            # Flag off => legacy Proposal-009 inference, byte-identical.
            _disclosure_on = False
            try:
                from core.config_policy import tool_progressive_disclosure
                _disclosure_on = tool_progressive_disclosure()
            except Exception:
                _disclosure_on = False
            if not _disclosure_on:
                # Proposal 009 option B: no (valid) explicit tools — infer from the goal's own text.
                inferred = _infer_tools_from_text(params.title, params.body, params.acceptance)
                if inferred:
                    logger.info("goal_create: inferred tools %s from goal text", sorted(inferred))
        if allowed or inferred:
            payload["tools"] = sorted(set(allowed) | inferred | set(_SELF_GOAL_BASELINE_TOOLS))
        board = self._resolve_board()
        parent_id = None
        if params.objective_id:
            obj = board.get(params.objective_id, user_id=user_id)
            if obj is None or obj.kind != "objective":
                return ActionResult(error=f"Cannot create goal: objective `{params.objective_id}` not found.",
                                    include_in_memory=True)
            parent_id = obj.id
        if params.acceptance:
            payload["acceptance"] = params.acceptance
        if params.acceptance_checks is not None:
            from agents.task.runtime.acceptance_checks import validate_checks
            try:
                payload["acceptance_checks"] = validate_checks(params.acceptance_checks)
            except (ValueError, TypeError) as exc:
                return ActionResult(error=f"Cannot create goal: invalid acceptance checks: {exc}",
                                    include_in_memory=True)
        try:
            goal = board.create(
                user_id=user_id, title=params.title, body=params.body, priority=params.priority,
                parent_id=parent_id, payload=payload or None,
                depends_on=params.depends_on,
            )
        except DuplicateGoalError as e:
            return ActionResult(
                error=(f"Duplicate: near-identical to goal `{e.match_id}` '{e.match_title}' "
                       f"(similarity {e.similarity:.2f}). Extend that goal instead, or change scope."),
                include_in_memory=True)
        except ValueError as e:
            # T2.1 Task 4: board.create validates depends_on BEFORE the row is
            # written (all-or-nothing), so a bad dep (unknown id / cross-tenant /
            # wrong kind) never leaves an orphan goal row — this surfaces as a
            # plain error result, same as any other create-time ValueError.
            return ActionResult(error=f"Cannot create goal: {e}", include_in_memory=True)
        tool_note = f" tools={payload['tools']}" if payload.get("tools") else ""
        dep_note = f" depends_on={board.dependencies(goal.id)}" if params.depends_on else ""
        drop_note = _dropped_tools_note(params.tools)
        mismatch_warning = _compute_tool_mismatch_warning(payload.get("tools"), params.acceptance_checks)
        warn_note = f"\n{mismatch_warning}" if mismatch_warning else ""
        return ActionResult(extracted_content=f"Created goal `{goal.id}` (status={goal.status}){tool_note}{dep_note}: {goal.title}{drop_note}{warn_note}",
                            include_in_memory=True)

    @BaseTool.action("List your durable goals, newest first — LIVE ones by default "
                     "(pass status='done'/'cancelled' for history).",
                     param_model=GoalListAction)
    async def goal_list(self, params: GoalListAction, execution_context=None) -> ActionResult:
        # A VIEW must read newest-first over the tenant's rows. ``board.list`` is the
        # dispatcher's ``priority DESC, created_at ASC LIMIT`` order: used here it
        # showed the OLDEST 100 rows of a 409-row prod board and zero manifest stream
        # legs (priority 2/3 sort after every priority-5 row), so the agent told the
        # owner it had no trading goals while ten clean cycles had run (2026-08-29).
        board = self._resolve_board()
        user_id = self._user(execution_context)
        statuses = (params.status,) if params.status else GOAL_LIST_LIVE_STATUSES
        goals = board.list_recent(user_id=user_id, statuses=statuses, limit=params.limit)
        counts = board.status_counts(user_id=user_id)
        totals = ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "0 rows"
        if not goals:
            what = f"status={params.status}" if params.status else "live"
            return ActionResult(
                extracted_content=f"No {what} goals. Board totals: {totals}.",
                include_in_memory=True)
        head = (f"Durable goals (newest first, showing {len(goals)} "
                f"{'status=' + params.status if params.status else 'live'}; "
                f"board totals: {totals}):")
        lines = [f"- `{g.id}` [{g.status}] p{g.priority}: {g.title}" for g in goals]
        return ActionResult(extracted_content=head + "\n" + "\n".join(lines),
                            include_in_memory=True)

    @BaseTool.action("Show one goal's detail + recent events.", param_model=GoalShowAction)
    async def goal_show(self, params: GoalShowAction, execution_context=None) -> ActionResult:
        board = self._resolve_board()
        user_id = self._user(execution_context)
        g = board.get(params.goal_id, user_id=user_id)
        if not g:
            return ActionResult(error="Goal not found.", include_in_memory=True)
        payload = g.payload or {}
        lines = [
            f"Goal `{g.id}` [{g.status}] p{g.priority}",
            f"Title: {g.title}",
            f"Failures: {g.consecutive_failures}/{g.max_retries}",
            f"Result: {(g.result or '')[:500]}",
        ]
        # §5.2/§5.3 stewardship: the agent sees its goal's full contract + attempt
        # history so it can maintain its pipeline and its user's picture of it.
        if payload.get("acceptance"):
            lines.append(f"Acceptance: {str(payload['acceptance'])[:300]}")
        if payload.get("acceptance_checks"):
            lines.append(f"Typed checks: {payload['acceptance_checks']}")
        if payload.get("outcome"):
            lines.append(f"Outcome: {str(payload['outcome'])[:300]}")
        if g.last_failure_error:
            lines.append(f"Last failure: {str(g.last_failure_error)[:300]}")
        attempts = payload.get("attempts") or []
        if attempts:
            lines.append("Attempts:")
            for a in attempts[-5:]:
                if isinstance(a, dict):
                    lines.append(f"  - {str(a.get('error') or '')[:200]}")
        # T2.1 Task 4: surface DAG edges (when any exist) — what this goal is
        # still waiting on, and what it in turn blocks. Capped at 10 ids (review
        # Minor; mirrors the attempts[-5:] compact-tail precedent above) so a
        # goal with a wide fan-out never blows the message out.
        def _id_title_list(ids: List[str]) -> str:
            capped = ids[:10]
            parts = []
            for dep_id in capped:
                dep_goal = board.get(dep_id, user_id=user_id)  # a foreign id shows as '?', never its title
                parts.append(f"{dep_id} ({dep_goal.title if dep_goal else '?'})")
            text = ", ".join(parts)
            if len(ids) > len(capped):
                text += f" (+{len(ids) - len(capped)} more)"
            return text

        deps = board.dependencies(g.id)
        if deps:
            lines.append("waiting on: " + _id_title_list(deps))
        blocks = board.dependents(g.id)
        if blocks:
            lines.append("blocks: " + _id_title_list(blocks))
        return ActionResult(extracted_content="\n".join(lines), include_in_memory=True)

    @BaseTool.action("Requeue a BLOCKED goal with a rationale (§5.3 stewardship; "
                     "resets its retry budget).", param_model=GoalUnblockAction)
    async def goal_unblock(self, params: GoalUnblockAction, execution_context=None) -> ActionResult:
        refusal = _autonomy_refusal(execution_context)
        if refusal:
            return refusal
        board = self._resolve_board()
        ok = board.unblock(params.goal_id, user_id=self._user(execution_context),
                           rationale=params.rationale)
        if not ok:
            return ActionResult(error=f"Cannot unblock `{params.goal_id}`: not a blocked goal of yours.",
                                include_in_memory=True)
        return ActionResult(
            extracted_content=f"Unblocked goal `{params.goal_id}` (retry budget reset).",
            include_in_memory=True)

    @BaseTool.action("Cancel a durable goal.", param_model=GoalCancelAction)
    async def goal_cancel(self, params: GoalCancelAction, execution_context=None) -> ActionResult:
        refusal = _autonomy_refusal(execution_context)
        if refusal:
            return refusal
        ok = self._resolve_board().cancel(params.goal_id, user_id=self._user(execution_context))
        return ActionResult(
            extracted_content=(f"Cancelled goal `{params.goal_id}`." if ok else "Nothing to cancel."),
            include_in_memory=True,
        )

    @BaseTool.action("Add a standing OBJECTIVE the planner decomposes into goals "
                     "(owner-only; abstract like 'get 100k followers' or concrete "
                     "like 'promote the v0.4.2 release').", param_model=ObjectiveAddAction)
    async def objective_add(self, params: ObjectiveAddAction, execution_context=None) -> ActionResult:
        refusal = _autonomy_refusal(execution_context)
        if refusal:
            return refusal
        try:
            _payload = {"success_criteria": params.success_criteria.strip()} \
                if params.success_criteria.strip() else None
            o = self._resolve_board().create_objective(
                user_id=self._user(execution_context), title=params.title,
                body=params.body, priority=params.priority, payload=_payload)
        except ValueError as e:
            return ActionResult(error=f"Cannot add objective: {e}", include_in_memory=True)
        return ActionResult(extracted_content=f"Created objective `{o.id}` [active]: {o.title}",
                            include_in_memory=True)

    @BaseTool.action("List standing objectives with per-status child-goal counts.",
                     param_model=ObjectiveListAction)
    async def objective_list(self, params: ObjectiveListAction, execution_context=None) -> ActionResult:
        board = self._resolve_board()
        objs = board.objectives(user_id=self._user(execution_context), status=params.status)
        if not objs:
            return ActionResult(extracted_content="No objectives.", include_in_memory=True)
        lines = []
        for o in objs:
            kids = board.children(o.id)
            counts = {}
            for k in kids:
                counts[k.status] = counts.get(k.status, 0) + 1
            summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no goals yet"
            lines.append(f"- `{o.id}` [{o.status}] p{o.priority}: {o.title} ({summary})")
        return ActionResult(extracted_content="Objectives:\n" + "\n".join(lines),
                            include_in_memory=True)

    @BaseTool.action("Pause/activate/drop a standing objective (owner-only).",
                     param_model=ObjectiveSetStatusAction)
    async def objective_set_status(self, params: ObjectiveSetStatusAction,
                                   execution_context=None) -> ActionResult:
        refusal = _autonomy_refusal(execution_context)
        if refusal:
            return refusal
        status = _OBJ_STATUS_MAP.get(params.status)
        if status is None:
            return ActionResult(error="status must be pause | activate | drop",
                                include_in_memory=True)
        ok = self._resolve_board().set_objective_status(
            params.objective_id, status, user_id=self._user(execution_context))
        return ActionResult(
            extracted_content=(f"Objective `{params.objective_id}` -> {status}." if ok
                               else "No such objective."),
            include_in_memory=True)

    @BaseTool.action("Edit a goal's title/body/priority/acceptance/tools (owner-only).",
                     param_model=GoalUpdateAction)
    async def goal_update(self, params: GoalUpdateAction, execution_context=None) -> ActionResult:
        refusal = _autonomy_refusal(execution_context)
        if refusal:
            return refusal
        patch: dict = {}
        if params.acceptance is not None:
            patch["acceptance"] = params.acceptance
        if params.tools is not None:
            patch["tools"] = [t for t in params.tools if t in allowed_self_goal_tools()]
        ok = self._resolve_board().update_fields(
            params.goal_id, user_id=self._user(execution_context),
            title=params.title, body=params.body, priority=params.priority,
            payload_patch=patch or None)
        return ActionResult(
            extracted_content=(f"Updated goal `{params.goal_id}`." if ok
                               else "Goal not found / terminal / nothing to change."),
            include_in_memory=True)


def goals_enabled() -> bool:
    from core.config_policy import AutonomyConfig
    return AutonomyConfig.goals_enabled()


def register_goal_tool(force: bool = False) -> bool:
    """Register the 'goal' descriptor + class IFF GOALS_ENABLED (or forced).

    Delegates to ``register_optional_tool`` (single shared factory). No-op when goals
    are off, so default deploys are unaffected. ``goal`` is never in the default
    ``tool_ids`` — agents opt in.
    """
    from tools.descriptors import (
        ToolDescriptor,
        ToolCategory,
        register_optional_tool,
    )

    return register_optional_tool(
        "goal",
        GoalTool,
        ToolDescriptor(
            name="goal",
            description=("Record/list/update/cancel durable cross-session goals and standing "
                        "objectives (goal_create/list/show/update/cancel, "
                        "objective_add/list/set_status)"),
            category=ToolCategory.INTEGRATION,
            required_config=[],
            init_priority=80,
            is_optional=True,
        ),
        goals_enabled,
        force=force,
    )
