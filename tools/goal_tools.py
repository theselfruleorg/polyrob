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
# (and — per owner — posting) but NOT self-grant money (wallet/x402/hyperliquid/polymarket),
# code execution, cron, or meta goal/skill tools. Mirrors CHILD_INHERITABLE_TOOLS + web_fetch + twitter.
_SELF_GOAL_ALLOWED_TOOLS = frozenset(
    {"filesystem", "task", "browser", "perplexity", "mcp", "anysite", "coding", "web_fetch", "twitter"}
)


class GoalCreateAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., description="Short title for the durable goal.", min_length=3)
    body: str = Field("", description="Full instructions for pursuing the goal (what 'done' looks like).")
    priority: int = Field(5, ge=1, le=10, description="1-10, higher runs first.")
    tools: Optional[List[str]] = Field(
        None,
        description=("Optional tools this goal may use (e.g. ['filesystem','coding','web_fetch',"
                     "'twitter']). Filtered to a safe allowlist; money/code-exec/cron are never granted."),
    )
    objective_id: Optional[str] = Field(None, description="Parent objective this goal advances.")
    acceptance: Optional[str] = Field(None, description="What 'done' must prove (ids/paths/urls).")


class GoalListAction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = Field(None, description="Filter: triage/ready/running/blocked/done/cancelled.")


class GoalShowAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(..., description="The goal id.")


class GoalCancelAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(..., description="The goal id to cancel.")


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
            data_dir = getattr(self.config, "data_dir", "data") if getattr(self, "config", None) else "data"
            self._goal_board = GoalBoard(os.path.join(data_dir, "goals.db"))
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
        if params.tools:
            allowed = [t for t in params.tools if t in _SELF_GOAL_ALLOWED_TOOLS]
            dropped = [t for t in params.tools if t not in _SELF_GOAL_ALLOWED_TOOLS]
            if dropped:
                logger.info("goal_create: dropped non-allowlisted tools %s (kept %s)", dropped, allowed)
            if allowed:
                payload["tools"] = allowed
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
        msg = (f"Goal `{g.id}` [{g.status}] p{g.priority}\n"
               f"Title: {g.title}\nFailures: {g.consecutive_failures}/{g.max_retries}\n"
               f"Result: {(g.result or '')[:500]}")
        return ActionResult(extracted_content=msg, include_in_memory=True)

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
            patch["tools"] = [t for t in params.tools if t in _SELF_GOAL_ALLOWED_TOOLS]
        ok = self._resolve_board().update_fields(
            params.goal_id, user_id=self._user(execution_context),
            title=params.title, body=params.body, priority=params.priority,
            payload_patch=patch or None)
        return ActionResult(
            extracted_content=(f"Updated goal `{params.goal_id}`." if ok
                               else "Goal not found / terminal / nothing to change."),
            include_in_memory=True)


def goals_enabled() -> bool:
    from agents.task.constants import AutonomyConfig
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
