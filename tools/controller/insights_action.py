"""The read-only `insights` action (W7), extracted per the god-file ratchet.

⚠️ Registry-closure landmine: NO `from __future__ import annotations` in this
module — the registry introspects the closure's first-param annotation to route
the validated param model (see GLM live-test bug 2026-06-20).
"""
from pydantic import BaseModel

from agents.task.agent.views import ActionResult


def register_insights_action(controller) -> None:
	"""Register the read-only `insights` tool (W7), gated INSIGHTS_TOOL.

	Reports whether the agent's self-authored skills actually get reused — the
	measurement the writable-skills safety brief requires. Tenant-scoped, no writes.
	"""
	try:
		from core.config_policy import AutonomyConfig
		if not AutonomyConfig.insights_tool():
			return
	except Exception:
		return

	class InsightsAction(BaseModel):
		pass

	@controller.registry.action(
		"Show insights about your own learning: how many durable skills you've "
		"authored and how often you reuse them (authored-skill reuse rate).",
		param_model=InsightsAction,
	)
	async def insights(params: InsightsAction, execution_context=None) -> ActionResult:
		user_id = getattr(execution_context, 'user_id', None) or getattr(controller, 'user_id', None)
		try:
			from modules.skills.skill_usage import get_skill_usage_store
			summary = get_skill_usage_store().authored_reuse_summary(user_id=user_id)
		except Exception as e:
			controller.logger.debug(f"insights failed: {e}")
			return ActionResult(extracted_content="No insights available.", include_in_memory=False)
		rate = round(summary["reuse_rate"] * 100)
		top = ", ".join(f"{t['skill_id']}({t['loads']})" for t in summary["top"][:5]) or "—"
		return ActionResult(
			extracted_content=(
				f"## Skill insights\n"
				f"- authored skills: {summary['authored_total']}\n"
				f"- reused at least once: {summary['authored_reused']} ({rate}%)\n"
				f"- by author: {summary['by_author']}\n"
				f"- most-used: {top}"
			),
			include_in_memory=True,
		)
