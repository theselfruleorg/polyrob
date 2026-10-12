"""The ONE builder of the policy hook chain (W5, owner decision D3).

Every pre/post tool-call hook that decides policy is installed from HERE, in
:data:`ORDER`. Before W5 the chain was assembled in two tiers — the Controller
constructor (``tools/controller/service.py``) and the agent's construction
(``agents/task/agent/core/construction.py``) — with no single statement of the
order. The order is the combination rule: pre-hooks run in order and the FIRST
refusal answers (``tools/controller/hooks.py``); a ``closed`` hook that raises
refuses.

Three entry points, one chain:

- :func:`install_controller_policy` — the Controller tier, from the ctor (so a
  delegated sub-agent's fresh Controller carries every hook).
- ``core.security.policy_order.install_session_gates`` — the agent tier's
  session gates (the correspondent and room gates). The agent tier owns their
  state (the orchestrator) and sits below this module, so it hands the hooks to
  the tier-0 installer, which applies the same :data:`ORDER`.
- :func:`seat_pipeline` / :func:`run_seat_action` — an owner SEAT that calls a verb
  directly (an HTTP ``/execute`` route, Telegram ``/lp``) runs the same
  denylist, effect and refusal-taint hooks a Controller call runs, so a surface
  cannot bypass them. The approval hooks are not part of a seat call: the owner
  at an authenticated seat IS the approver (an action card's Confirm re-runs the
  stored line through the seat).

NOTE: no ``from __future__ import annotations`` (the Controller mixin rule).
"""
import os

from tools.controller._helpers import make_denylist_hook
from core.security.taints import correspondent_probe as _correspondent_probe

#: The chain's order and the seat subset live in tier-0 (the agent tier reads
#: them too): ``core.security.policy_order``.
from core.security.policy_order import ORDER, SEAT_STAGES  # noqa: E402,F401


def _resolve_denylist(controller, pref_data_home):
	"""POLYROB_TOOL_DENYLIST UNION the owner's ``approvals.deny`` prefs."""
	_denylist_env = os.getenv("POLYROB_TOOL_DENYLIST", "").strip()
	_denylist = [n.strip() for n in _denylist_env.split(",") if n.strip()]
	try:
		from core import prefs as _prefs
		_denylist = list(_prefs.resolve(
			"approvals.deny", controller.user_id, pref_data_home,
			env_value=_denylist, default=_denylist,
		) or [])
	except Exception as e:
		controller.logger.error(f"denylist pref union skipped (non-fatal): {e}")
	return _denylist


def _pref_home(controller):
	from core.runtime_paths import data_dir_or_home
	try:
		return data_dir_or_home(
			getattr(getattr(controller.container, "config", None), "data_dir", None))
	except Exception:
		return data_dir_or_home(None)


def install_controller_policy(controller):
	"""Register the Controller tier of :data:`ORDER` on *controller*."""
	# owner-UX P1 T5: per-tenant home dir for pref resolution below — mirrors
	# the SELF_CONTEXT block's data_dir source (construction.py):
	# container.config.data_dir, falling back to "data". Resolved once,
	# fail-open, so a Mock/absent container never breaks Controller construction.
	_pref_data_home = _pref_home(controller)

	# owner-UX P1 T5: `approvals.deny` prefs UNION into the operator denylist —
	# additive only (a pref can ADD a never-run action, never remove one the
	# operator configured via POLYROB_TOOL_DENYLIST). No pref file => the env list.
	_denylist = _resolve_denylist(controller, _pref_data_home)
	if _denylist:
		controller.register_pre_tool_call_hook(
			make_denylist_hook(_denylist),
			fail_mode="closed",  # a crashing guardrail must DENY, not silently allow
		)

	from tools.controller.wallet_authority import make_wallet_authority_hook
	controller.register_pre_tool_call_hook(make_wallet_authority_hook(controller), fail_mode="closed")

	# 022: the send-time gate. Every outbound verb (tools/controller/
	# financial_claim_gate.py::OUTBOUND_TEXT_FIELDS) that claims money moved
	# must be backed by a settled record, or the send is refused by name.
	from tools.controller.financial_claim_gate import make_financial_claim_hook
	controller.register_pre_tool_call_hook(make_financial_claim_hook(controller), fail_mode="closed")

	# A refused money verb holds the rest of its run off every public rail:
	# the record hook taints the run, the gate refuses a non-owner send
	# (tools/controller/refusal_taint_gate.py). Owner sends still go.
	from tools.controller.refusal_taint_gate import (
		make_refusal_taint_gate_hook, make_refusal_taint_record_hook)
	controller.register_pre_tool_call_hook(make_refusal_taint_gate_hook(controller), fail_mode="closed")
	controller.register_post_tool_call_hook(make_refusal_taint_record_hook(controller), fail_mode="open")

	# 033: the ONE effect classification. The gate (pre, fail-closed) refuses an
	# autonomous social/comms/public/money write while the owner's pause covers
	# it, BEFORE any approval ask is queued; the recorder (post, fail-open) writes
	# one `external_write` row per outward act, on the failure path too.
	# Registered here, never on an instance: a delegated sub-agent builds a fresh
	# Controller, so a hook attached post-hoc would miss delegated work.
	from tools.controller.effect_hooks import make_effect_gate_hook, make_effect_record_hook
	controller.register_pre_tool_call_hook(make_effect_gate_hook(controller), fail_mode="closed")
	controller.register_post_tool_call_hook(make_effect_record_hook(controller), fail_mode="open")

	# Approval seam (Item 7E): gate the resolved action set through an
	# ApprovalProvider (default AutoApprover = allow). Empty set => no-op.
	# owner-UX P2 T4: the full composition — the FROZEN env+posture set
	# (WS-6/WS-7: `APPROVAL_REQUIRED_TOOLS`/`APPROVAL_PROVIDER` snapshotted at
	# import so a mid-process env mutation can't flip gating; posture >= 2
	# UNIONs the compute-tool gated set and defaults the provider to
	# interactive) UNIONed with owner `approvals.require` prefs, provider
	# tightened by `approvals.provider` (stricter-of, never looser) — now
	# lives in ONE helper, `effective_approval_state()`
	# (tools/controller/approval.py). The `/approve` REPL command +
	# `polyrob approvals` CLI call the SAME helper for display, so what the
	# owner is shown can never drift from what's enforced here. No pref file
	# => byte-identical to the pre-extraction inline composition.
	from tools.controller.approval import effective_approval_state
	_gates, _provider_name = effective_approval_state(controller.user_id, _pref_data_home)
	# The other execution rails reach a shell too: when shell_run is gated they
	# follow ITS rule (owner for the dangerous class), but an action with its own
	# gate keeps it. `_inherited` lets the exemption apply the shell rule only there.
	_inherited = ()
	if "shell_run" in _gates:
		_gates = dict(_gates)
		_inherited = tuple(a for a in ("coding_run_tests", "code_execution_run_code")
						   if a not in _gates)
		for _execution_action in _inherited:
			_gates[_execution_action] = _gates["shell_run"]
	_required = set(_gates.keys())
	# 073 W2: the shell command guard — BEFORE every approval hook, so a floor
	# command or an unattended dangerous one is refused without filing an owner
	# ask. Registered unconditionally (the floor holds at every posture);
	# `_shell_exempt` narrows a gated shell_run's owner wait to the dangerous
	# class (SHELL_APPROVAL_MODE=every restores the wait on every call).
	from tools.controller.command_guard_hook import (
		make_command_guard_hook, make_shell_command_exemption,
		orchestrator_llm_factory, shell_approval_mode, shell_lists)
	_shell_mode = shell_approval_mode()
	_shell_allow, _shell_deny = shell_lists(controller.user_id, _pref_data_home)
	controller.register_pre_tool_call_hook(
		make_command_guard_hook(
			shell_gated="shell_run" in _required, deny_globs=_shell_deny,
			mode=_shell_mode, llm_factory=orchestrator_llm_factory(controller.orchestrator)),
		fail_mode="closed",
	)
	_shell_exempt = make_shell_command_exemption(_shell_mode, allow_globs=_shell_allow,
												 inherited=_inherited)
	if _required:
		# H9: importing this module registers the 'interactive_cli' provider so an
		# operator can actually select it via APPROVAL_PROVIDER.
		try:
			import tools.controller.approval_interactive  # noqa: F401
		except Exception:
			pass
		from tools.controller.approval import (
			approval_wait_timeout_sec, get_approval_provider_or_deny, make_approval_hook)
		if _provider_name == "auto_notify":
			# 013 T4: act-and-report under effective AUTONOMY_MODE=autonomous —
			# TWO lanes instead of the single supervised hook. The always-gated
			# self-modification verbs + owner `approvals.require` pref pins stay
			# on the durable, remotely approvable owner_queue provider; the rest
			# of the gated set is allowed by auto_notify and reported post-hoc
			# (audit event + one owner notification per successful run). The
			# supervised path below stays byte-identical.
			try:
				# Importing this module registers the 'owner_queue' provider
				# (mirrors the payment wiring at :240 below).
				import tools.controller.approval_queue  # noqa: F401
				from tools.controller.approval import autonomous_gating_lanes
				from tools.controller.approval_queue import (
					OwnerQueueApprover, make_tool_auto_notify_hook)
				_queued, _reported = autonomous_gating_lanes(_gates)
				# Finding 2 (013 T4 review): PAYMENT_APPROVAL_TOOLS (x402_request,
				# hyperliquid/polymarket order verbs) already get their OWN
				# first-class pre-hook (mode="approve" -> owner_queue) + post-hoc
				# notify (mode="auto") wired below via `payment_approval_mode()` —
				# never ALSO route them through this generic reported lane, or a
				# within-cap payment under PAYMENT_APPROVAL_MODE=auto gets a
				# second, misleading "[auto-approved]" notification even when the
				# owner explicitly approved it (mode="approve"). They stay
				# ELIGIBLE for the owner_queue lane above if they're ever
				# `_ALWAYS_GATED_VERBS`/pref-pinned (they aren't today) — this
				# only strips them out of `_reported`, never `_queued`.
				try:
					from core.config_policy import PAYMENT_APPROVAL_TOOLS as _PAY_TOOLS
					_reported -= set(_PAY_TOOLS)
				except Exception as e:
					controller.logger.error(
						f"Failed to exclude PAYMENT_APPROVAL_TOOLS from the "
						f"auto_notify reported lane: {e}")
				_orch = controller.orchestrator
				from core.security.taints import correspondent_probe
				_taint_probe = correspondent_probe(_orch)
				if _queued:
					_q_provider = get_approval_provider_or_deny(
						"owner_queue", user_id=controller.user_id, home_dir=_pref_data_home,
					)
					# Same Finding-1 taint short-circuit as the payment wiring:
					# a tainted turn is denied by owner_queue itself — no durable
					# ask, no owner notification.
					if isinstance(_q_provider, OwnerQueueApprover):
						_q_provider.set_taint_probe(_taint_probe)
					controller.register_pre_tool_call_hook(
						# 030 WS-E2 (L10): the durable queue gets the remote
						# round-trip timeout, never the in-process 30s default.
						make_approval_hook(
							_q_provider, _queued,
							timeout=approval_wait_timeout_sec("owner_queue"),
							exempt_fn=_shell_exempt),
						fail_mode="closed",  # approval failure must DENY
					)
				if _reported:
					_r_provider = get_approval_provider_or_deny(
						"auto_notify", user_id=controller.user_id, home_dir=_pref_data_home,
					)
					controller.register_pre_tool_call_hook(
						make_approval_hook(_r_provider, _reported),
						fail_mode="closed",
					)
					controller.register_post_tool_call_hook(
						make_tool_auto_notify_hook(
							controller.container, _reported, taint_probe=_taint_probe),
						fail_mode="open",  # a notify failure must never break the caller
					)
				controller.logger.info(
					f"🔐 Autonomous approval lanes: owner_queue={sorted(_queued)} "
					f"auto_notify={sorted(_reported)}"
				)
			except Exception as e:
				controller.logger.error(f"Failed to wire autonomous approval lanes: {e}")
				controller._deny_all_fallback(_required, "autonomous approval lanes")
		else:
			# H9: fail-CLOSED. An unknown APPROVAL_PROVIDER resolves to deny-by-default
			# (not skip-registration), so a misconfigured provider can never silently
			# leave the requested tools UNGATED.
			# owner-UX P2 T5: thread tenant context so the interactive ladder's
			# [a]lways-allow/[n]ever branches can read/write this tenant's
			# preferences.toml. Providers that don't accept these kwargs
			# (AutoApprover/DenyByDefaultApprover) ignore them (see get_approval_provider).
			_provider = get_approval_provider_or_deny(
				_provider_name, user_id=controller.user_id, home_dir=_pref_data_home,
			)
			try:
				controller.register_pre_tool_call_hook(
					# 030 WS-E2 (L10): timeout follows the provider — a durable
					# owner_queue wait gets the remote round-trip budget.
					make_approval_hook(
						_provider, _required,
						timeout=approval_wait_timeout_sec(_provider_name),
						exempt_fn=_shell_exempt),
					fail_mode="closed",  # approval failure must DENY
				)
				controller.logger.info(
					f"🔐 Approval gating enabled for: {sorted(_required)} "
					f"(provider={type(_provider).__name__})"
				)
			except Exception as e:
				controller.logger.error(f"Failed to wire approval gating: {e}")
				controller._deny_all_fallback(_required, "approval gating")

	# Payment approval mode (Task 9 / G-2, T7 review fix): outward-facing
	# payment-CREATION actions (core.config_policy.PAYMENT_APPROVAL_TOOLS)
	# are gated FIRST-CLASS — independent of the generic APPROVAL_REQUIRED_TOOLS/
	# APPROVAL_PROVIDER seam above, which stays exactly as it was for every
	# non-payment tool. PAYMENT_APPROVAL_TOOLS splits into two lanes:
	# PAYMENT_RECEIVE_APPROVAL_TOOLS (x402_request) is eligible for act-and-report
	# under mode="auto"; everything else (the live-trade order verbs) is SPEND-side
	# and ALWAYS keeps owner_queue pre-approval regardless of mode — the hard
	# product line is money-spend/trading is never act-and-report. See
	# agents/task/constants.py::payment_approval_mode / PAYMENT_RECEIVE_APPROVAL_TOOLS.
	try:
		from core.config_policy import (
			PAYMENT_APPROVAL_TOOLS, PAYMENT_RECEIVE_APPROVAL_TOOLS, payment_approval_mode)
		# 023 §5.3 D3: the per-CALL predicate that lets a simulation, and (only
		# under DEFI_TIERED_SPEND_LANE) a within-ceiling live spend, skip the
		# owner tap. Returns None for everything else, so the lane below is
		# unchanged for every other verb and every other deployment.
		from core.config_policy.spend_lane import (
			DEFI_SPEND_VERBS, autonomous_ceiling_usd, is_simulation,
			spend_exemption, tiered_spend_lane_enabled)
		# The same predicate with the turn in view: a genuine owner turn that
		# transfers to an address that is OURS waits on no second tap.
		from tools.controller.self_transfer import spend_exemption_with_turn
		_payment_tools = set(PAYMENT_APPROVAL_TOOLS)
		_receive_tools = _payment_tools & set(PAYMENT_RECEIVE_APPROVAL_TOOLS)
		_spend_tools = _payment_tools - _receive_tools
	except Exception as e:
		controller.logger.error(f"Failed to resolve PAYMENT_APPROVAL_TOOLS: {e}")
		_payment_tools = set()
		_receive_tools = set()
		_spend_tools = set()
		# CR-L20: an unresolvable lane has no tool list to deny, so deny
		# every MONEY action instead — never leave them ungated.
		controller._deny_money_fallback("payment approval lane resolution")
	if _payment_tools:
		_pay_mode = payment_approval_mode()
		if _pay_mode == "approve":
			try:
				# Importing this module registers the 'owner_queue' provider
				# (mirrors the H9 'interactive_cli' self-registration above).
				import tools.controller.approval_queue  # noqa: F401
				from tools.controller.approval import (
					get_approval_provider_or_deny, make_approval_hook)
				from core.config_policy import payment_approval_timeout_sec
				from tools.controller.approval_queue import OwnerQueueApprover
				_pay_provider = get_approval_provider_or_deny(
					"owner_queue", user_id=controller.user_id, home_dir=_pref_data_home,
				)
				# Finding 1 (fix pass 1): give owner_queue the SAME
				# correspondent-taint probe correspondent_gate reads
				# (`orchestrator._correspondent_tainted`) so a tainted turn is
				# denied by owner_queue itself — no durable ask, no owner
				# notification — regardless of hook registration order vs
				# correspondent_gate (registered later, in agent construction).
				# `get_approval_provider_or_deny` is the generic factory (only
				# threads user_id/home_dir) — this Controller.__init__ is the
				# site that actually has the orchestrator reference, so the
				# probe is injected post-construction via the setter seam. Only
				# a real OwnerQueueApprover gets it (a monkeypatched/custom
				# 'owner_queue' provider under test is left untouched).
				if isinstance(_pay_provider, OwnerQueueApprover):
					_orch = controller.orchestrator
					_pay_provider.set_taint_probe(
						_correspondent_probe(_orch)
					)
				controller.register_pre_tool_call_hook(
					make_approval_hook(_pay_provider, _payment_tools,
					                   timeout=payment_approval_timeout_sec(),
					                   exempt_fn=spend_exemption_with_turn),
					fail_mode="closed",  # approval failure must DENY
				)
				controller.logger.info(
					f"💳 Payment approval mode=approve: {sorted(_payment_tools)} "
					"-> owner_queue"
				)
			except Exception as e:
				controller.logger.error(f"Failed to wire payment approval gating (spend lane): {e}")
				controller._deny_all_fallback(_payment_tools, "payment approval gating")
		else:  # "auto" — spend verbs STILL owner_queue pre-approved (hard line);
			# only the receive-side subset act-and-reports.
			if _spend_tools:
				try:
					# Importing this module registers the 'owner_queue' provider
					# (mirrors the H9 'interactive_cli' self-registration above).
					import tools.controller.approval_queue  # noqa: F401
					from tools.controller.approval import (
						get_approval_provider_or_deny, make_approval_hook)
					from core.config_policy import payment_approval_timeout_sec
					from tools.controller.approval_queue import OwnerQueueApprover
					_pay_provider = get_approval_provider_or_deny(
						"owner_queue", user_id=controller.user_id, home_dir=_pref_data_home,
					)
					# Finding 1 (fix pass 1): give owner_queue the SAME
					# correspondent-taint probe correspondent_gate reads
					# (`orchestrator._correspondent_tainted`) so a tainted turn is
					# denied by owner_queue itself — no durable ask, no owner
					# notification — regardless of hook registration order vs
					# correspondent_gate (registered later, in agent construction).
					# `get_approval_provider_or_deny` is the generic factory (only
					# threads user_id/home_dir) — this Controller.__init__ is the
					# site that actually has the orchestrator reference, so the
					# probe is injected post-construction via the setter seam. Only
					# a real OwnerQueueApprover gets it (a monkeypatched/custom
					# 'owner_queue' provider under test is left untouched).
					if isinstance(_pay_provider, OwnerQueueApprover):
						_orch = controller.orchestrator
						_pay_provider.set_taint_probe(
							_correspondent_probe(_orch)
						)
					controller.register_pre_tool_call_hook(
						make_approval_hook(_pay_provider, _spend_tools,
						                   timeout=payment_approval_timeout_sec(),
						                   exempt_fn=spend_exemption_with_turn),
						fail_mode="closed",  # approval failure must DENY
					)
					controller.logger.info(
						f"💳 Payment approval mode=auto (spend lane): {sorted(_spend_tools)} "
						"-> owner_queue"
					)
				except Exception as e:
					controller.logger.error(f"Failed to wire payment approval gating (spend lane): {e}")
					controller._deny_all_fallback(_spend_tools, "payment approval gating")
			if _receive_tools:
				try:
					from tools.controller.approval_queue import make_payment_auto_notify_hook
					# Finding 1 (fix pass 1): SAME taint short-circuit as the
					# 'approve' branch above — mode=auto emits no owner
					# notification/audit for a tainted turn either.
					_orch = controller.orchestrator
					controller.register_post_tool_call_hook(
						make_payment_auto_notify_hook(
							controller.container, _receive_tools,
							taint_probe=_correspondent_probe(_orch),
						),
						fail_mode="open",  # a notify failure must never break the caller
					)
					controller.logger.info(
						f"💳 Payment approval mode=auto: {sorted(_receive_tools)} "
						"-> post-execution owner notify"
					)
				except Exception as e:
					controller.logger.error(f"Failed to wire payment auto-notify hook: {e}")

		# 023 D3: an autonomously-executed on-chain spend is act-and-report, never
		# silent. The SAME post-hoc rail the receive lane uses fires one owner
		# notification + a `payment_auto_approved` audit event per EXECUTED trade.
		# Dry runs are skipped -- they succeed loudly but move nothing, so pinging
		# the owner for each simulation would train them to ignore the channel.
		if _spend_tools and tiered_spend_lane_enabled():
			try:
				from tools.controller.approval_queue import make_payment_auto_notify_hook
				_orch = controller.orchestrator
				_tiered_notify = _spend_tools & set(DEFI_SPEND_VERBS)
				controller.register_post_tool_call_hook(
					make_payment_auto_notify_hook(
						controller.container, _tiered_notify,
						taint_probe=_correspondent_probe(_orch),
						skip_fn=is_simulation,  # H03a: a verb with no dry_run is never one
						# 039 Unit A: audit event only. The owner MESSAGE now comes
						# from core/wallet/tx_notify.py, which knows the chain, the
						# amounts, the hash, the cap headroom and the settled
						# outcome. This hook only ever knew the action name.
						audit_only=True,
					),
					fail_mode="open",  # a notify failure must never break the caller
				)
				controller.logger.info(
					f"💳 Tiered spend lane ON: {sorted(_tiered_notify)} execute below "
					f"${autonomous_ceiling_usd():.2f} -> payment_auto_approved audit "
					f"(owner message via tx_notify)"
				)
			except Exception as e:
				controller.logger.error(f"Failed to wire tiered spend-lane notify: {e}")



class _SeatController:
	"""What the shared hooks read from a Controller, for one owner-seat call:
	the tool that owns the action, the principal, and no session/orchestrator."""

	def __init__(self, *, tool_id, user_id):
		import logging
		from types import SimpleNamespace
		self.user_id = user_id
		self.orchestrator = None
		self.container = None
		self.session_id = ""
		self._is_sub_agent = False
		self.logger = logging.getLogger("tools.controller.policy_chain.seat")
		self._details = SimpleNamespace(tool=tool_id, function=None)

	def get_action_details(self, name):
		return self._details


def seat_pipeline(*, tool_id, user_id):
	"""A HookPipeline holding :data:`SEAT_STAGES`, for an owner-seat call."""
	from tools.controller.hooks import HookPipeline
	from tools.controller.effect_hooks import make_effect_gate_hook, make_effect_record_hook
	from tools.controller.refusal_taint_gate import (
		make_refusal_taint_gate_hook, make_refusal_taint_record_hook)
	controller = _SeatController(tool_id=tool_id, user_id=user_id)
	pipe = HookPipeline(controller.logger)
	denied = _resolve_denylist(controller, _pref_home(controller))
	if denied:
		pipe.register_pre(make_denylist_hook(denied), "closed")
	pipe.register_pre(make_refusal_taint_gate_hook(controller), "closed")
	pipe.register_post(make_refusal_taint_record_hook(controller), "open")
	pipe.register_pre(make_effect_gate_hook(controller), "closed")
	pipe.register_post(make_effect_record_hook(controller), "open")
	return pipe


class SeatRefused(Exception):
	"""A seat call the policy chain refused; ``str(exc)`` is the refusal."""


async def run_seat_action(*, tool_id, action, params, execution_context, call):
	"""Run ``await call()`` for an owner seat between the seat stages of the chain.

	*action* is the registered action name (``{tool}_{verb}``) the hooks key on.
	Raises :class:`SeatRefused` when a pre-hook refuses; the post-hooks see the
	result (and an exception's error) exactly as a Controller call's would."""
	pipe = seat_pipeline(tool_id=tool_id,
	                     user_id=str(getattr(execution_context, "user_id", "") or ""))
	reason = await pipe.run_pre(action, params, execution_context)
	if reason:
		raise SeatRefused(reason)
	try:
		result = await call()
	except Exception as exc:
		from types import SimpleNamespace
		await pipe.run_post(action, params, SimpleNamespace(error=str(exc)), execution_context)
		raise
	await pipe.run_post(action, params, result, execution_context)
	return result


__all__ = ["ORDER", "SEAT_STAGES", "install_controller_policy", "seat_pipeline",
           "run_seat_action", "SeatRefused"]
