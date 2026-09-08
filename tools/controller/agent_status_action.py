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
from core.runtime_paths import data_dir_or_home

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

	_cfg = getattr(getattr(controller, 'container', None), 'config', None)
	data_dir = data_dir_or_home(getattr(_cfg, 'data_dir', None))
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


def register_agent_status_action(controller) -> None:
	"""Register the read-only `agent_status` introspection tool (I-6), gated
	AGENT_STATUS_TOOL (default false; ON under POLYROB_LOCAL)."""
	from core.config_policy import AutonomyConfig
	if not AutonomyConfig.agent_status_tool():
		return

	class AgentStatusAction(BaseModel):
		pass

	@controller.registry.action(
		"Report your own runtime state: health (credit sentinel, open asks, blocked "
		"goals, suppressed owner messages, dead loops), steps used/remaining, active "
		"tools, context usage, and wallet/ledger balance. Read-only.",
		param_model=AgentStatusAction,
	)
	async def agent_status(params: AgentStatusAction, execution_context=None) -> ActionResult:
		user_id = getattr(execution_context, 'user_id', None) or getattr(controller, 'user_id', None)
		lines = []
		# 0) the shared status snapshot — health FIRST, then every section
		#    (the ledger is awaited natively here and handed to the snapshot so
		#    both views are built from ONE read; balances: display surface).
		ledger = None
		if user_id:
			try:
				from modules.credits.unified_ledger import build_ledger
				ledger = await build_ledger(user_id, include_balances=True)
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
				session_id=getattr(orch, 'session_id', None), ledger=ledger)
			lines.extend(render_status_lines(snap, prefix="- "))
		except Exception as e:
			lines.append(_unavail("status snapshot", e))
		# 1) steps used / budget — live AgentState
		agent = None
		try:
			agent = _resolve_agent(controller, execution_context)
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
		# 4) wallet — the agent's own (operator-owned singleton) wallet
		try:
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
