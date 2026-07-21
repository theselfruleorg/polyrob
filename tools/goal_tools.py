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
_SELF_GOAL_ALLOWED_TOOLS = frozenset(
    {"filesystem", "task", "browser", "perplexity", "mcp", "anysite", "coding", "web_fetch",
     "twitter", "email", "message", "x402_invoice", "knowledge"}
)


def allowed_self_goal_tools() -> frozenset:
    """Tool ids an agent-created goal may request. Under effective autonomous mode
    (AUTONOMY_MODE=autonomous on a single-owner instance) this expands to the full
    AUTONOMOUS_MODE_TOOLS grant; money-spend/host tools are in NEITHER set."""
    try:
        from agents.task.constants import AUTONOMOUS_MODE_TOOLS
        from core.config_policy import full_autonomy_enabled
        if full_autonomy_enabled():
            return frozenset(_SELF_GOAL_ALLOWED_TOOLS | set(AUTONOMOUS_MODE_TOOLS))
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
    acceptance: Optional[str] = Field(None, description="What 'done' must prove (ids/paths/urls).")
    acceptance_checks: Optional[List[Any]] = Field(
        None,
        description=("Optional TYPED checks the framework executes at run end (fail-closed "
                     "when present). ONLY these types exist — do NOT invent others: "
                     "[{'type':'artifact_glob','pattern':'*.md'}, "
                     "{'type':'http_ok','url':'https://…'}, "
                     "{'type':'file_contains','path':'report.md','contains':['A','B'],"
                     "'mode':'all'}]. Prefer setting one when the outcome is mechanically "
                     "checkable."),
    )


class GoalListAction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = Field(None, description="Filter: triage/ready/running/blocked/done/cancelled.")


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

    @BaseTool.action("Record a DURABLE goal pursued across sessions (beyond this turn). "
                     "Use for ongoing objectives; use `task` for this-turn TODOs and `cronjob` "
                     "for time-scheduled runs.", param_model=GoalCreateAction)
    async def goal_create(self, params: GoalCreateAction, execution_context=None) -> ActionResult:
        user_id = self._user(execution_context)
        payload: dict = {}
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
            obj = board.get(params.objective_id)
            if obj is None or obj.kind != "objective" or obj.user_id != user_id:
                return ActionResult(error=f"Cannot create goal: objective `{params.objective_id}` not found.",
                                    include_in_memory=True)
            parent_id = obj.id
        if params.acceptance:
            payload["acceptance"] = params.acceptance
        if params.acceptance_checks:
            # §4.4: optional sharpener — keep only well-formed {'type': str, ...}
            # dicts; malformed entries are dropped (never a create gate).
            typed = [c for c in params.acceptance_checks
                     if isinstance(c, dict) and str(c.get("type") or "").strip()]
            if typed:
                payload["acceptance_checks"] = typed[:10]
        try:
            goal = board.create(
                user_id=user_id, title=params.title, body=params.body, priority=params.priority,
                parent_id=parent_id, payload=payload or None,
            )
        except DuplicateGoalError as e:
            return ActionResult(
                error=(f"Duplicate: near-identical to goal `{e.match_id}` '{e.match_title}' "
                       f"(similarity {e.similarity:.2f}). Extend that goal instead, or change scope."),
                include_in_memory=True)
        except ValueError as e:
            return ActionResult(error=f"Cannot create goal: {e}", include_in_memory=True)
        tool_note = f" tools={payload['tools']}" if payload.get("tools") else ""
        return ActionResult(extracted_content=f"Created goal `{goal.id}` (status={goal.status}){tool_note}: {goal.title}",
                            include_in_memory=True)

    @BaseTool.action("List your durable goals (optionally filtered by status).",
                     param_model=GoalListAction)
    async def goal_list(self, params: GoalListAction, execution_context=None) -> ActionResult:
        goals = self._resolve_board().list(user_id=self._user(execution_context), status=params.status)
        if not goals:
            return ActionResult(extracted_content="No durable goals.", include_in_memory=True)
        lines = [f"- `{g.id}` [{g.status}] p{g.priority}: {g.title}" for g in goals]
        return ActionResult(extracted_content="Durable goals:\n" + "\n".join(lines), include_in_memory=True)

    @BaseTool.action("Show one goal's detail + recent events.", param_model=GoalShowAction)
    async def goal_show(self, params: GoalShowAction, execution_context=None) -> ActionResult:
        board = self._resolve_board()
        g = board.get(params.goal_id)
        if not g or g.user_id != self._user(execution_context):
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
