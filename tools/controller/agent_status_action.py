"""The read-only `agent_status` action (I-6), extracted per the god-file ratchet
(2026-08-28, status SSOT) — `action_registration.py` is at its size ceiling.

⚠️ Registry-closure landmine: NO `from __future__ import annotations` in this
module — the registry introspects the closure's first-param annotation to route
the validated param model (see GLM live-test bug 2026-06-20).

The agent's own view of "how am I doing" renders from the SAME snapshot the
owner's Telegram `/status`, `polyrob doctor` and the webview /system page
render (`core/status_snapshot.py`), so the agent and the owner cannot give
different answers to the same question in the same minute. Health comes first.
The live-agent-only facts (steps, loaded tools, context tokens, wallet
addresses/balance, the detailed two-statement ledger, resolved prefs, the
tool catalog) follow. Every section fails SOFT independently — and soft means
REPORTED (`<section>: unavailable (<reason>)`), never hidden.
"""
import logging

from pydantic import BaseModel

from agents.task.agent.views import ActionResult
from core.runtime_paths import data_dir_or_home, prefs_home_dir
from tools.harness_view import gated_tools_block, tool_harness_view

logger = logging.getLogger(__name__)


def _resolve_agent(controller, execution_context):
	orch = getattr(controller, 'orchestrator', None)
	if orch is None or not getattr(orch, 'agents', None):
		return None
	agent_id = getattr(execution_context, 'agent_id', None)
	if agent_id and agent_id in orch.agents:
		return orch.agents[agent_id]
	agents = list(orch.agents.values())
	return agents[0] if agents else None


def _unavail(section: str, exc: BaseException) -> str:
	return f"{section}: unavailable ({type(exc).__name__}: {str(exc)[:120]})"


async def _wallet_lines() -> list:
	from core.wallet.factory import get_agent_wallet
	lines = []
	wallet = get_agent_wallet()
	if wallet is None:
		return ["wallet: off"]
	if wallet.config.network == "mainnet":
		from core.wallet.onchain import balances, venue_chain
		addr = wallet.operational_signer().address
		native, usdc = balances(addr, venue_chain(wallet.operational_venue) or "base")
		u = f"${usdc:.2f}" if usdc is not None else "unavailable"
		g = f"{native:.5f}" if native is not None else "unavailable"
		lines.append(f"wallet: {u} USDC | gas {g}")
	# Own addresses, both families (2026-08-27): the agent must be able to see
	# WHO it is on-chain — Solana included — or it reports/funds the wrong identity.
	parts = []
	try:
		parts.append(f"evm {wallet.operational_signer().address}")
	except Exception as e:
		parts.append(f"evm unavailable ({type(e).__name__})")
	try:
		sol = wallet.solana_address
		if sol:
			parts.append(f"solana {sol}")
	except Exception as e:
		parts.append(f"solana unavailable ({type(e).__name__})")
	lines.append("wallet_addresses: " + " | ".join(parts))
	return lines


def _config_lines(controller, user_id) -> list:
	# Secret hygiene: nothing here reads a raw env VALUE — pref values are
	# schema-guaranteed non-secret, posture/autonomy accessors return
	# enums/ints/bools; the block is still scrubbed as a defensive backstop.
	from core.prefs import PREF_SCHEMA, display_effective
	from core.secret_scrub import scrub_secret_shapes
	from core.instance import resolve_instance_id
	from core.config_policy import (
		compute_posture, autonomy_posture, autonomy_mode_display,
		local_mode_enabled, AutonomyConfig,
	)
	from tools.cronjob_tools import cron_enabled as _cron_enabled

	data_dir = prefs_home_dir()  # the ONE identity home (prefs writers use it)
	instance_id = resolve_instance_id()
	cfg_lines = ["config:"]
	cfg_lines.append(
		f"  posture: compute={compute_posture()} autonomy={autonomy_posture()} "
		f"mode={autonomy_mode_display()} local={local_mode_enabled()}"
	)
	cfg_lines.append(
		"  autonomy_loops: goals={} cron={} self_wake={} digest={}".format(
			AutonomyConfig.goals_enabled(), _cron_enabled(),
			AutonomyConfig.self_wake_enabled(), AutonomyConfig.owner_digest_enabled(),
		)
	)
	by_group: dict = {}
	for key in sorted(PREF_SCHEMA):
		by_group.setdefault(key.split(".", 1)[0], []).append(key)
	for group in sorted(by_group):
		cfg_lines.append(f"  [{group}]")
		for key in by_group[group]:
			value, source = display_effective(key, user_id, data_dir, instance_id)
			cfg_lines.append(f"    {key} = {value} ({source})")
	return [scrub_secret_shapes("\n".join(cfg_lines))]


async def _detail_lines(controller, user_id, ledger) -> list:
	"""The gated half (AGENT_STATUS_TOOL): wallet, the two-statement ledger and
	the resolved config. Each section fails soft and REPORTED."""
	lines = []
	# 4) wallet — the agent's own (operator-owned singleton) wallet.
	#    CR-M07: its balances and addresses are the OWNER's facts; a
	#    non-owner tenant's turn is told they are withheld (never hidden).
	try:
		from core.wallet.authority import owner_refusal
		_refusal = owner_refusal(user_id)
		if _refusal:
			lines.append(f"wallet: withheld ({_refusal})")
		else:
			lines.extend(await _wallet_lines())
	except Exception as e:
		lines.append(_unavail("wallet", e))
	# 5) the detailed two-statement ledger (never summed)
	try:
		if isinstance(ledger, BaseException):
			raise ledger
		from modules.credits.unified_ledger import format_ledger
		lines.append(format_ledger(ledger))
	except Exception as e:
		lines.append(_unavail("ledger", e))
	# 6) resolved config / prefs
	try:
		lines.extend(_config_lines(controller, user_id))
	except Exception as e:
		lines.append(_unavail("config", e))
	return lines


async def _self_lines(agent, execution_context) -> list:
	"""095 W7 (X-6, SK-11): the facts a "which version / model are you, what does
	this run cost" answer needs — the running version, a pending deploy, THIS
	chat's model (provider, serving provider, reasoning level) and, on a run with
	a per-run cap, its spend vs the cap. Computed here (tools tier), never in
	``core/status_*``. Each line fails soft and REPORTED."""
	out = []
	try:
		from core.version import deployed_from, pending_deploy_line, running_version_line
		out.append(f"running: {running_version_line()}")
		pending = pending_deploy_line()
		if pending:
			out.append(pending)
		if deployed_from().get("previous"):
			out.append("what changed since the previous deploy: "
					   "agent_status(release_notes='since_previous')")
	except Exception as e:
		out.append(_unavail("running", e))
	try:
		if agent is None or not getattr(agent, "model_name", None):
			out.append("this chat's model: unavailable (no live agent)")
		else:
			from modules.llm.usage_extract import resolve_serving_provider
			from agents.task.model_pin_state import session_reasoning
			model = agent.model_name
			provider = (getattr(agent, "llm_provider", None)
						or getattr(agent, "provider_name", None) or "unknown")
			serving = resolve_serving_provider(getattr(agent, "llm", None), model) or "unknown"
			orch = getattr(agent, "orchestrator", None)
			sid = getattr(orch, "session_id", None) or getattr(agent, "session_id", None)
			level = session_reasoning(sid) or "operator default"
			out.append(f"this chat's model: {model} (provider {provider}; served by "
					   f"{serving}; reasoning {level})")
	except Exception as e:
		out.append(_unavail("this chat's model", e))
	try:
		pair = getattr(agent, "_run_spend_seen", None) if agent is not None else None
		if pair is None and agent is not None:
			from agents.task.agent.core.run_budget import run_spend_vs_cap
			pair = await run_spend_vs_cap(agent)
		if pair:
			spent, cap = float(pair[0]), float(pair[1])
			out.append(f"run_spend: ${spent:.4f} of the ${cap:.2f} per-run cap "
					   f"({spent / cap * 100:.0f}%)" if cap > 0 else f"run_spend: ${spent:.4f}")
	except Exception as e:
		out.append(_unavail("run_spend", e))
	try:
		out.append(_owner_line(agent, execution_context))
	except Exception as e:
		out.append(_unavail("owner", e))
	return out


def _owner_line(agent, execution_context) -> str:
	"""095 W7 (SK-15): who the owner is BY KIND and where this turn sits — never a
	real identifier (no id, no handle, no address)."""
	from core.instance import resolve_instance_id, resolve_owner_principal
	from core.surfaces.catalog import owner_env_by_surface
	import os
	bound = resolve_owner_principal(default_to_instance=False)
	if not bound:
		kind = "unpaired (no owner bound on this install)"
	elif bound == resolve_instance_id():
		kind = "bound to this instance's own tenant"
	else:
		kind = "a bound owner principal"
	reach = [sid for sid, env in sorted(owner_env_by_surface().items())
			 if (os.environ.get(env) or "").strip()]
	if "telegram" not in reach:
		from core.instance import resolve_owner_telegram_id
		if resolve_owner_telegram_id():
			reach.append("telegram")
	seat = "unknown"
	try:
		from agents.task.session_class import session_regime
		if agent is not None:
			seat = session_regime(agent)
	except Exception as exc:
		seat = f"unavailable ({type(exc).__name__})"
	user_id = getattr(execution_context, "user_id", None)
	if user_id is not None:
		from core.instance import is_owner
		who = "the owner" if is_owner(user_id, owner_principal=resolve_owner_principal()) \
			else "not the owner"
	else:
		who = "unknown"
	return (f"owner: {kind}; reachable on {', '.join(reach) or 'no configured surface'}; "
			f"this session's seat: {seat}; this turn's user: {who}")


def _may_propose(controller, execution_context) -> bool:
	"""093 review: whether ``propose_setting`` would run on this turn — the SAME
	D4 check the verb makes, so the tool view never suggests a refused call. The
	verb's registrar leaves the check on the controller (no import edge: it would
	close a cycle); no verb registered = nothing to suggest."""
	check = getattr(controller, "propose_setting_refusal", None)
	if execution_context is None or not callable(check):
		return False
	try:
		return check(execution_context) is None
	except Exception:
		return False


def _tool_view(controller, tool_id: str, execution_context) -> str:
	"""093 C1: one tool's harness view + the running version and a pending
	owner deploy (the facts a "why is X missing" answer needs)."""
	is_leaf = (getattr(execution_context, 'role', None) == 'leaf'
			   or bool(getattr(execution_context, 'is_sub_agent', False)) or None)
	try:
		out = [tool_harness_view(controller, tool_id, is_leaf=is_leaf,
								 may_propose=_may_propose(controller, execution_context))]
	except Exception as e:
		out = [_unavail(f"tool {tool_id}", e)]
	try:
		from core.version import running_version_line
		out.append(f"running: {running_version_line()}")
	except Exception as e:
		out.append(_unavail("running", e))
	try:
		from core.lifecycle import deploy_wait_line
		_cfg = getattr(getattr(controller, 'container', None), 'config', None)
		line = deploy_wait_line(data_dir_or_home(getattr(_cfg, 'data_dir', None)))
		if line:
			out.append(line)
	except Exception as e:
		out.append(_unavail("deploy", e))
	return "\n".join(out)


def register_agent_status_action(controller) -> None:
	"""Register the read-only `agent_status` introspection tool (I-6).

	080 WS-5 (D5): the read-only part — the status snapshot (health, work,
	loops/next), steps, tools, context, release notes — is registered on EVERY
	posture. The wallet, ledger and resolved-config detail stay gated
	AGENT_STATUS_TOOL (default false; ON under POLYROB_LOCAL)."""
	from core.config_policy import AutonomyConfig
	detail = AutonomyConfig.agent_status_tool()

	class AgentStatusAction(BaseModel):
		release_notes: str = ""
		tool: str = ""

	_money = ("tools, context usage, and wallet/ledger balance" if detail
			  else "tools and context usage")
	@controller.registry.action(
		"Report your own runtime state: health (credit sentinel, open asks, blocked "
		"goals, suppressed owner messages, dead loops), what runs next, steps "
		f"used (of the cap), active {_money}. Read-only. Set release_notes to "
		"'unreleased' (what changed since the last release), 'since_previous' (what "
		"changed since the previous deploy), 'latest' or a version "
		"('1.2.0') to read instead what shipped, from the CHANGELOG.md shipped with "
		"your code. Set tool to a tool id to read why that tool is missing: its status, "
		"remedy, who can fix it and when the fix applies.",
		param_model=AgentStatusAction,
	)
	async def agent_status(params: AgentStatusAction, execution_context=None) -> ActionResult:
		if (params.release_notes or "").strip():
			from core.version import release_notes, running_version_line
			return ActionResult(
				extracted_content=f"running: {running_version_line()}\n"
				+ release_notes(params.release_notes),
				include_in_memory=True,
			)
		if (params.tool or "").strip():
			return ActionResult(extracted_content=_tool_view(controller, params.tool.strip(),
															  execution_context),
								include_in_memory=True)
		user_id = getattr(execution_context, 'user_id', None) or getattr(controller, 'user_id', None)
		lines = []
		# 0) the shared status snapshot — health FIRST, then every section
		#    (the ledger is awaited natively here and handed to the snapshot so
		#    both views are built from ONE read; balances: display surface).
		ledger = None
		if not detail:
			pass  # money detail gated: the snapshot below skips the money read
		elif user_id:
			try:
				from modules.credits.unified_ledger import build_ledger
				ledger = await build_ledger(user_id, days=1, include_balances=True)
			except Exception as e:
				ledger = e
		else:
			ledger = ValueError("no tenant (empty user_id)")
		_cfg = getattr(getattr(controller, 'container', None), 'config', None)
		try:
			import asyncio
			from core.status_snapshot import build_status_snapshot
			from core.status_render import render_status_lines
			orch = getattr(controller, 'orchestrator', None)
			snap = await asyncio.to_thread(
				build_status_snapshot, str(user_id or ""),
				data_dir=getattr(_cfg, 'data_dir', None),
				container=getattr(controller, 'container', None),
				session_id=getattr(orch, 'session_id', None), ledger=ledger,
				include_money=detail)
			# 093 D1: the snapshot's `harness` slot is the OWNER-seat view; this
			# session's own view (its loads, ceiling, leaf) follows below.
			from core.status_snapshot import SECTION_ORDER
			lines.extend(render_status_lines(
				snap, prefix="- ", sections=[n for n in SECTION_ORDER if n != "harness"]))
		except Exception as e:
			lines.append(_unavail("status snapshot", e))
		# 094: which model a new owner chat, an autonomous and a money-class run use.
		from agents.task.agent.core.live_health import model_routing_line
		_models = model_routing_line(str(user_id or ""))
		if _models:
			lines.append(f"- {_models}")
		# 1) steps used / budget — live AgentState
		agent = None
		try:
			agent = _resolve_agent(controller, execution_context)
		except Exception as e:
			lines.append(_unavail("agent", e))
		# 095 W7: version, pending deploy, this chat's model, run spend, owner fact.
		lines.extend(await _self_lines(agent, execution_context))
		try:
			st = getattr(agent, 'state', None)
			if st is not None:
				mx = getattr(st, 'max_steps', None)
				lines.append(f"steps: {st.n_steps}/{mx if mx is not None else '?'}")
			else:
				lines.append("steps: unavailable (no live agent state)")
		except Exception as e:
			lines.append(_unavail("steps", e))
		# 2) active tools
		try:
			tools = sorted(controller.list_tools())
			lines.append("tools: " + (", ".join(tools) if tools else "(none)"))
		except Exception as e:
			lines.append(_unavail("tools", e))
		# 3) context-token usage
		try:
			mm = getattr(agent, 'message_manager', None)
			if mm is not None:
				used = mm.get_token_count()
				max_in = getattr(mm, 'max_input_tokens', 0) or 0
				if max_in > 0:
					lines.append(f"context_tokens: {used}/{max_in} ({used / max_in * 100:.0f}%)")
				else:
					lines.append(f"context_tokens: {used}")
			else:
				lines.append("context_tokens: unavailable (no message manager)")
		except Exception as e:
			lines.append(_unavail("context_tokens", e))
		if detail:
			lines.extend(await _detail_lines(controller, user_id, ledger))
		else:
			lines.append("wallet, ledger, config: withheld on this posture "
						 "(AGENT_STATUS_TOOL off) — say so; do not guess them")
		# 093 C1: every gated tool with who can fix it and when the fix applies.
		try:
			block = gated_tools_block(controller)
			if block:
				lines.append(block)
		except Exception as e:
			lines.append(_unavail("harness", e))
		# 7) capabilities — ground-truth tool availability (S1 catalog)
		try:
			is_leaf = getattr(execution_context, 'role', None) == 'leaf'
			lines.append(controller.render_tool_catalog(is_leaf=is_leaf))
		except Exception as e:
			lines.append(_unavail("capabilities", e))
		return ActionResult(
			extracted_content="\n".join(lines) or "status unavailable",
			include_in_memory=True,
		)
