"""031: the owner-only `autonomy_control` action — the agent's hand on the ONE
pause record (``core.autonomy_control``), so "stop trading for 6 hours" said in
chat becomes durable STATE every loop reads, not a row cancellation the next
stream cycle undoes.

Registered UNCONDITIONALLY by the Controller (``tools/controller/service.py``,
next to ``_register_default_actions``) — not a tool_id, so it is present in
every owner session regardless of GOALS_ENABLED / CRON_ENABLED. Extracted per
the god-file ratchet (``action_registration.py`` is at its size ceiling).

Gates (all fail-closed): the caller must be the OWNER principal (or the local
operator under POLYROB_LOCAL) and the turn must be genuine (never a self-wake /
delegation-result / sub-agent / autonomous-run turn). A correspondent-tainted
session cannot reach this action at all — the WS-A capability gate
(``agents/task/agent/core/correspondent_gate.py``) lists it as high-impact — so
third-party data can neither pause nor resume; the owner uses /pause or /resume.

⚠️ Registry-closure landmine: NO `from __future__ import annotations` in this
module — the registry introspects the closure's first-param annotation to route
the validated param model (GLM live-test bug 2026-06-20).
"""
import logging
import os
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from agents.task.agent.views import ActionResult
from core.runtime_paths import data_dir_or_home

logger = logging.getLogger(__name__)

#: ⚠️ These ARE ``core.autonomy_control.SCOPES``, and
#: ``tests/unit/tools/controller/test_autonomy_action_scopes.py`` pins the two
#: together: a scope the record accepts but this Literal rejects is a pause the
#: owner can write from every seat EXCEPT by asking the agent.
_SCOPE = Literal["all", "trading", "streams", "planner", "cron", "social",
                 "oversight", "pings", "apps"]


class AutonomyControlAction(BaseModel):
	action: Literal["pause", "resume", "status"]
	scopes: List[_SCOPE] = Field(default_factory=lambda: ["all"])
	duration_minutes: Optional[int] = Field(default=None, ge=1, le=60 * 24 * 30)
	cancel_goals: bool = False
	reason: str = ""


def register_autonomy_control_action(controller) -> None:
	"""Register `autonomy_control` on *controller*'s registry (unconditional)."""

	@controller.registry.action(
		"Stop your own autonomous work (goal dispatch, planner, stream seeding, cron, "
		"self-wake, social posts, the dev/ops loops) — durable across restarts. Call "
		"this FIRST when the owner asks you to stop, pause or halt anything, then quote "
		"the result verbatim. scopes narrows it (default all); duration_minutes makes it "
		"temporary; cancel_goals=true also cancels the ready goal rows the pause held. "
		"Cancelling goals alone is NOT a stop. You cannot lift a pause — only the owner "
		"can, with /resume; if they ask you to start again, tell them that.",
		param_model=AutonomyControlAction,
	)
	async def autonomy_control(params: AutonomyControlAction, execution_context=None) -> ActionResult:
		from core.config_policy import local_mode_enabled
		from core.instance import is_owner_local_safe, resolve_owner_principal
		from core.surfaces.owner_admin import (pause_autonomy, pause_state,
		                                       render_pause_result)
		from tools.controller.turn_origin import _is_forged_or_autonomous_turn

		user_id = getattr(execution_context, "user_id", None) or getattr(controller, "user_id", None)
		if not user_id:
			return ActionResult(error="autonomy_control: no tenant (empty user_id)",
			                    include_in_memory=True)
		_cfg = getattr(getattr(controller, "container", None), "config", None)
		data_dir = data_dir_or_home(getattr(_cfg, "data_dir", None))

		is_forged = bool(_is_forged_or_autonomous_turn(execution_context, controller))
		try:
			owner_ok = is_owner_local_safe(user_id, owner_principal=resolve_owner_principal(),
			                               local_enabled=local_mode_enabled())
		except Exception:
			logger.warning("autonomy_control: owner probe failed — refusing", exc_info=True)
			owner_ok = False
		if is_forged or not owner_ok:
			return ActionResult(
				error="autonomy_control is owner-only; a forged, autonomous or non-owner turn "
				      "cannot change the pause state (ask the owner to send /pause or /resume)",
				include_in_memory=True)

		if params.action == "status":
			from core.status_render import pause_headline_from
			return ActionResult(
				extracted_content=pause_headline_from(
					pause_state(data_dir).to_dict(), resume_hint="/resume (owner only)",
					pause_hint="autonomy_control(pause)"),
				include_in_memory=True)

		if params.action == "resume":
			# 034 §11.2: an owner SEAT lifts a pause, never the agent's reading of a
			# turn. On 2026-09-09 the owner's `/halt` was undone by this branch on
			# the words "a b and c approve" — an approval of three BRIDGE paths,
			# read as consent to resume everything — and a goal he had cancelled the
			# day before spent money 28 minutes later. The gate was on WHO and never
			# on WHAT. `pause` and `status` stay: stopping itself and reporting the
			# state are things the agent should be able to do.
			state = pause_state(data_dir)
			scope_txt = ", ".join(state.scopes) if state.paused else "nothing"
			return ActionResult(
				extracted_content=(
					f"Refused: I cannot resume autonomy — only you can. Currently paused: "
					f"{scope_txt}.\nSend `/resume` (or `/resume <scope>`) when you want it "
					f"back. I can still pause myself and report the state."),
				include_in_memory=True)

		res = pause_autonomy(data_dir, scopes=tuple(params.scopes),
		                     duration_minutes=params.duration_minutes,
		                     reason=(params.reason or "").strip(), via="agent")
		text = render_pause_result(res, resume_hint="/resume (owner only)",
		                           status_hint="autonomy_control(status)", chat=False)
		if params.cancel_goals and res.effective:
			try:
				from agents.task.goals.board import GoalBoard
				board = GoalBoard(os.path.join(data_dir, "goals.db"))
				# board.list is a LIMIT-bounded dispatch window: loop until it drains.
				n = 0
				for _ in range(200):
					batch = board.list(user_id=user_id, status="ready")
					if not batch:
						break
					got = sum(1 for g in batch if board.cancel(g.id, user_id=user_id))
					n += got
					if not got:
						break
				text += f"\nCancelled {n} held goal row(s)."
			except Exception as e:
				text += f"\nCould not cancel goal rows: {type(e).__name__}: {str(e)[:120]}"
		return ActionResult(extracted_content=text, include_in_memory=True)
