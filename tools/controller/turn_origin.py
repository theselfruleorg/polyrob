"""Turn-origin policy helpers (extracted from action_registration.py, 2026-08-20).

Pure module-level predicates/builders — no action closures, so the registry's
first-param-annotation introspection never sees this module. External callers
(crypto_trade_gate, defi/trade_tool, approval_queue, tests) import these via
``tools.controller.action_registration``, which re-exports them; lazy
``from ... import`` at call time reads that module's namespace, so existing
monkeypatches keep working.
"""
import logging

from core.security.forged_turns import FORGED_TURN_KINDS as _FORGED_TURN_KINDS

logger = logging.getLogger(__name__)


def _is_forged_or_autonomous_turn(execution_context, controller_self):
	"""C7/W2/SK-F10: a turn that must NEVER auto-activate or promote its own content.

	True for a sub-agent / leaf turn (delegated worker, background-review reviewer),
	an autonomous goal/cron/planner-spawned session, OR a forged self-wake /
	async-delegation-result re-entry into the MAIN agent.

	SK-F10: a self-wake or delegation-result re-entry resolves to
	role='orchestrator', is_sub_agent=False — the SAME shape as a genuine owner
	turn — so without a turn-kind signal it could auto-activate a skill (review
	off) or self-promote its own pending draft. The run loop stamps
	`execution_context.metadata["turn_kind"]` from the orchestrator's
	`_forged_turn_kind` marker (set when a drained HITL message is
	kind="self_wake"/"delegation_result", cleared on a genuine drained turn) —
	see `agents/task/agent/core/user_ingress.py::_drain_user_messages` and
	`agents/task/agent/core/step_execution.py::_build_execution_context`.
	"""
	is_sub = bool(getattr(execution_context, 'is_sub_agent', False)
	              or getattr(controller_self, '_is_sub_agent', False))
	# H7: default to the least-privileged role when unset (treat as forged).
	role = getattr(execution_context, 'role', 'leaf')
	if is_sub or role == 'leaf':
		return True
	metadata = getattr(execution_context, 'metadata', None) or {}
	if metadata.get('turn_kind') in _FORGED_TURN_KINDS:
		return True
	try:
		from agents.task.goals.autonomy_marker import is_autonomous
		sid = (getattr(execution_context, 'session_id', None)
		       or getattr(controller_self, 'session_id', ''))
		return bool(is_autonomous(sid))
	except Exception:
		# MH1: fail CLOSED — an autonomy-marker probe that raises must be treated
		# as forged (the least-privileged assumption), mirroring the fail-closed
		# role/turn_kind defaults above. Returning False here would let a
		# forged/autonomous turn slip past every gate keyed on this predicate
		# (owner_queue approval, writable-skills, message/self_context promotion).
		logger.debug(
			"_is_forged_or_autonomous_turn: autonomy-marker probe raised — "
			"treating as forged (fail-closed)", exc_info=True)
		return True


def _is_autonomous_goal_turn(execution_context, controller_self):
	"""True ONLY for a goal/cron-dispatched turn on the MAIN agent.

	Deliberately STRICTER than :func:`_is_forged_or_autonomous_turn`, which lumps
	every non-genuine turn together. `core/wallet/tx_guard.py` injects this as
	``autonomous_ok_fn`` to tell the one origin the owner armed for unattended
	treasury work apart from the origins that must NEVER sign: a leaf/sub-agent
	(a delegated worker can be steered by whatever it was handed), a self-wake,
	and a delegation-result re-entry (both re-enter the main agent wearing a
	genuine turn's shape, which is exactly why SK-F10 stamps `turn_kind`).

	Every uncertainty answers False, so an unprovable turn keeps the refusal.
	"""
	if bool(getattr(execution_context, 'is_sub_agent', False)
	        or getattr(controller_self, '_is_sub_agent', False)):
		return False
	# An unset role is treated as 'leaf' everywhere else; keep that here, so a
	# context that cannot prove it is the orchestrator does not get to sign.
	if getattr(execution_context, 'role', 'leaf') != 'orchestrator':
		return False
	metadata = getattr(execution_context, 'metadata', None) or {}
	if metadata.get('turn_kind') in _FORGED_TURN_KINDS:
		return False
	try:
		from agents.task.goals.autonomy_marker import is_autonomous
		sid = (getattr(execution_context, 'session_id', None)
		       or getattr(controller_self, 'session_id', ''))
		return bool(is_autonomous(sid))
	except Exception:
		logger.debug(
			"_is_autonomous_goal_turn: autonomy-marker probe raised — answering "
			"False (fail-closed)", exc_info=True)
		return False


def _autonomous_message_refusal(execution_context, controller_self):
	"""Gate for the `message` action on forged/autonomous turns.

	Returns a refusal ActionResult, or None when the send may proceed to the
	normal target-tier gate. Default (flag OFF) = blanket refusal, byte-identical
	to the pre-2026-07-14 behavior. With MESSAGE_AUTONOMOUS_ALLOWLISTED=true the
	autonomous send falls through — perform_message_send still denies any target
	that is not the owner or owner-ALLOWLISTED, so the owner-curated allowlist is
	the owner-in-the-loop mechanism (battle-test night-2 fix: the group-intro and
	mailbox goals could never send, regardless of allowlists).

	013 T6: ALSO falls through when the resolved outbound policy for this tenant
	is `open`/`domains` — under those policies perform_message_send's own tier
	gate (daily cap + seed-before-send + first-contact report) is the real
	owner-in-the-loop mechanism, so blanket-refusing an autonomous turn here
	would just be a second, redundant deny-by-default in front of it. This hook
	has no surface/home_dir to thread through (the `message` action's target
	surface is a per-call param, not known here) — resolve the env/mode-default
	layer only (home_dir=None skips prefs entirely; perform_message_send's own
	call still resolves the pref-aware effective policy downstream).
	"""
	if not _is_forged_or_autonomous_turn(execution_context, controller_self):
		return None
	try:
		from core.config_policy import message_autonomous_allowlisted
		if message_autonomous_allowlisted():
			return None
	except Exception:
		pass
	try:
		from core.surfaces.outbound_policy import resolve_outbound_policy
		user_id = getattr(execution_context, "user_id", None) or ""
		policy, _domains = resolve_outbound_policy(user_id, "", home_dir=None)
		if policy in ("open", "domains"):
			return None
	except Exception:
		pass
	from tools.controller.types import ActionResult
	return ActionResult(
		extracted_content=(
			"message: not permitted for forged/autonomous turns "
			"(owner must be in the loop; the owner can set "
			"MESSAGE_AUTONOMOUS_ALLOWLISTED=true to permit autonomous sends "
			"to owner-ALLOWLISTED targets only)"),
		include_in_memory=True)


_MESSAGE_TEXT_PREVIEW_CHARS = 200


def _message_action_result(res: dict, surface: str, target: str, text: str) -> "ActionResult":
	"""Build the `message` action's ActionResult from `perform_message_send`'s
	return dict (2026-07-19 fix).

	The completion judge's evidence pack (agents/task/runtime/evidence.py) is built
	straight from ActionResult.extracted_content/error — there is no separate
	content-tracking path. Before this fix, `extracted_content` carried only a
	generic "OK"/"FAILED" template with no reference to what was actually sent, and
	`error` was never set on failure either (a failed send looked identical to a
	success in the ledger). Live-observed: a goal whose acceptance criteria
	referenced the message's CONTENT was rejected as "no successful message action
	... with the changelog content recorded" despite the send genuinely succeeding
	— the evidence pack had no content signal to match against at all.

	Success now carries a bounded preview of the sent text; failure now sets a real
	`error` so it can no longer be mistaken for a success in the ledger.
	"""
	from tools.controller.types import ActionResult

	note = f" [{res['note']}]" if res.get('note') else ""
	# Overnight 2026-07-19 sibling fix: an attachment-blind result made the agent
	# resend ~12x and declare BLOCKED — success must NAME what rode the message.
	attached = (f" [attached {len(res['media_attached'])} file(s): "
	            f"{', '.join(res['media_attached'])}]"
	            if res.get('media_attached') else "")
	if res['success']:
		preview = (text or "")[:_MESSAGE_TEXT_PREVIEW_CHARS]
		if len(text or "") > _MESSAGE_TEXT_PREVIEW_CHARS:
			preview += "…"
		return ActionResult(
			extracted_content=(
				f"message[{res['tier']}] -> {surface}:{target} OK{attached}{note} | text: {preview!r}"),
			include_in_memory=True)
	return ActionResult(
		extracted_content=(
			f"message[{res['tier']}] -> {surface}:{target} FAILED: {res.get('error') or ''}{note}"),
		error=res.get('error') or 'message send failed',
		include_in_memory=True)
