"""`worker_manage` — the agent's read + propose verb over named workers (041 phase 2).

A worker is a reusable, named agent configuration the ORCHESTRATOR dispatches
with ``delegate_task(profile="<id>")``. This action lets the agent see the
approved ones and PROPOSE a new one. It never approves: whatever the agent
writes lands in ``.pending/`` (``created_by=background_review`` — forced
quarantine in ``ProfileStore``) and is undispatchable until the owner runs
``polyrob workers approve <id>`` or ``/workers approve <id>``.

Gated ``WORKERS_ENABLED`` (default OFF — nothing registers). Capability row
``worker_manage`` (``core/tool_capabilities.py``): ``high_impact`` (it writes a
prompt the parent later reads, so a correspondent-tainted session is denied)
and ``delegate_blocked`` (a leaf child never shapes the orchestrator's helpers;
the action also refuses a sub-agent / leaf caller outright).

⚠️ Deliberately NO ``from __future__ import annotations``: the Registry
introspects the closure's first-param model (the Controller-mixin rule).
"""
import logging
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from tools.controller.types import ActionResult

logger = logging.getLogger(__name__)


class WorkerManageAction(BaseModel):
	action: Literal["list", "show", "propose"]
	worker_id: Optional[str] = Field(
		default=None, description="Worker id (lowercase letters, digits, dashes).")
	description: Optional[str] = Field(
		default=None,
		description="propose: WHEN to use this worker, one or two sentences. The "
		            "orchestrator reads it to decide dispatch.")
	instructions: Optional[str] = Field(
		default=None, description="propose: how the worker should work (its standing brief).")
	tools: Optional[List[str]] = Field(
		default=None,
		description="propose: tool ids the worker may use. Never wider than yours; "
		            "money, exec and publish tools are stripped at dispatch.")
	max_steps: Optional[int] = Field(default=None, ge=1, le=50)


def _line(spec) -> str:
	tools = ", ".join(spec.tool_ids) if spec.tool_ids is not None else "inherits"
	return f"- {spec.id}: {spec.description or '(no description)'} (tools: {tools})"


def register_worker_manage_action(controller) -> None:
	"""Register ``worker_manage`` on *controller* when ``WORKERS_ENABLED``."""
	try:
		from agents.task.agent.profile_store import workers_enabled
		if not workers_enabled():
			return
	except Exception:
		return

	@controller.registry.action(
		"See and propose NAMED WORKERS — reusable helpers you dispatch with "
		"delegate_task(goal=..., profile='<id>'). action='list' shows approved and "
		"pending ids; 'show' one worker; 'propose' drafts a new one (description + "
		"instructions + tools). A proposal waits for the owner's approval and "
		"cannot be dispatched until then.",
		param_model=WorkerManageAction,
	)
	async def worker_manage(params: WorkerManageAction, execution_context=None) -> ActionResult:
		from agents.task.agent.profile_store import (
			PROVENANCE_BACKGROUND, WorkerSpec, build_worker_profile, get_store,
			is_valid_profile_id)
		user_id = (getattr(execution_context, "user_id", None)
		           or getattr(controller, "user_id", None))
		if not user_id:
			return ActionResult(error="workers need a user (tenant scope).",
			                    include_in_memory=True)
		if execution_context is not None and (
				getattr(execution_context, "is_sub_agent", False)
				or getattr(execution_context, "role", "") == "leaf"):
			return ActionResult(error="a delegated worker cannot manage workers.",
			                    include_in_memory=True)
		store = get_store()
		if params.action == "list":
			approved = store.list_approved(user_id)
			pending = store.list_pending(user_id)
			lines = [f"Approved workers ({len(approved)}):"]
			lines += [_line(WorkerSpec(m)) for m in approved] or ["- none"]
			lines.append(f"Pending owner approval ({len(pending)}): "
			             + (", ".join(pending) if pending else "none"))
			return ActionResult(extracted_content="\n".join(lines), include_in_memory=True)
		wid = (params.worker_id or "").strip()
		if not is_valid_profile_id(wid):
			return ActionResult(error="worker_id must match ^[a-z][a-z0-9-]{0,63}$.",
			                    include_in_memory=True)
		if params.action == "show":
			model = store.get_approved(wid, user_id=user_id)
			state = "approved"
			if model is None:
				model, state = store.get_pending(wid, user_id=user_id), "pending owner approval"
			if model is None:
				return ActionResult(error=f"no worker '{wid}'.", include_in_memory=True)
			spec = WorkerSpec(model)
			body = [_line(spec), f"state: {state}",
			        f"max_steps: {spec.max_steps or 'default'}",
			        f"model: {spec.model or 'inherits'}"]
			if spec.instructions:
				body.append("instructions:\n" + spec.instructions)
			return ActionResult(extracted_content="\n".join(body), include_in_memory=True)
		# propose — ALWAYS quarantined: the agent never approves its own helper.
		if not (params.description or "").strip():
			return ActionResult(error="propose needs a description (when to use it).",
			                    include_in_memory=True)
		data = build_worker_profile(
			wid, description=params.description or "", instructions=params.instructions or "",
			tools=params.tools, max_steps=params.max_steps)
		res = store.save_profile(data, user_id=user_id, created_by=PROVENANCE_BACKGROUND)
		if not res.ok:
			return ActionResult(error="propose failed: " + "; ".join(res.errors),
			                    include_in_memory=True)
		return ActionResult(
			extracted_content=(f"Worker '{wid}' proposed — it waits for the owner's approval "
			                   f"(/workers approve {wid}) and cannot be dispatched until then."),
			include_in_memory=True)
