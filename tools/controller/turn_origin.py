"""Turn-origin policy helpers (extracted from action_registration.py, 2026-08-20).

Pure module-level predicates/builders — no action closures, so the registry's
first-param-annotation introspection never sees this module. External callers
(crypto_trade_gate, defi/trade_tool, approval_queue, tests) import these via
``tools.controller.action_registration``, which re-exports them; lazy
``from ... import`` at call time reads that module's namespace, so existing
monkeypatches keep working.
"""
import logging
from typing import Optional

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
	# 044 T5: a room-bound (group) turn is forged too — whoever spoke, it must
	# never auto-activate/promote content the way a genuine owner turn can.
	if metadata.get('turn_kind') in _FORGED_TURN_KINDS or metadata.get('turn_kind') == 'group':
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


def _autonomous_owner_resend_cooldown_refusal(
		execution_context, controller_self, *, container, user_id: str,
		surface: str, target: str, owner_targets: dict,
		text: Optional[str] = None, event_log=...):
	"""2026-08-27 dedup-guard fix: an autonomous/forged turn proactively
	messaging the OWNER is rate-limited against resending within
	``owner_message_cooldown_seconds()`` of a real send to the SAME owner
	address on the SAME surface — checked against the durable conversation
	store's actual send history, never the model's own self-report of elapsed
	time.

	Confirmed live pattern this closes: a fresh goal session has no
	visibility into a SIBLING session's send from ~2h earlier, so each retry
	of a failed "deliver the owner ask" goal re-sent the identical content —
	4 genuine sends of the same consolidated ask landed in the owner's
	Telegram within ~4 hours, each with the goal's own narrative FALSELY
	claiming "24h cadence respected" (observed 2026-08-27).

	**The gate reads CONTENT** (2026-09-15 prod review, C5). It used to be a
	bare COUNT of any owner send in the window, which refused a materially NEW
	report because something unrelated had gone out within 2h. Prod, verbatim:

	    contact_history checked: last owner sends were 09:12 (telegram),
	    08:14, 07:35 (email) — ALL predate the guard-blocker discovery
	    (~09:40). The blocker is materially new, so the retry is justified per
	    the dedup guard's own guidance.
	    … Retried owner notice; suppressed again by the 2h Telegram dedup
	    despite being new content. … Ending BLOCKED.

	The agent followed the refusal's own instructions and was refused anyway;
	32 refusals in 7 days and the goal ended BLOCKED with the owner never
	told. So *text* is compared against the bodies actually sent in the
	window: a repeat is refused, a materially different report proceeds.
	Omitting *text* (or a store with no body reader) keeps the legacy
	count-only gate, so no caller silently loses its guard.

	A refusal is DURABLY RECORDED — a marked ``owner_notice`` plus a
	``user_delivery`` row with outcome ``cooldown`` — because this path
	bypasses the delivery rail entirely and so left no trace anywhere: the
	suppression was invisible to `/missed`, to the digest and to telemetry.

	Returns a refusal ActionResult, or None when the send may proceed.
	A genuine owner-initiated interactive turn is NEVER gated here — only
	forged/autonomous turns. Fail-open: any error here must never block a
	legitimate send.
	"""
	if not _is_forged_or_autonomous_turn(execution_context, controller_self):
		return None
	body = (text or "").strip()
	try:
		from core.config_policy import owner_message_cooldown_seconds
		cooldown = owner_message_cooldown_seconds()
		if cooldown <= 0:
			return None
		from tools.controller.message_send import _OWNER_ALIASES
		owner_addr = (owner_targets or {}).get(surface)
		is_owner_send = owner_addr is not None and (
			str(target) == str(owner_addr)
			or (isinstance(target, str) and target.strip().lower() in _OWNER_ALIASES))
		if not is_owner_send:
			return None
		store = container.get_service("conversation_store") if container else None
		if store is None:
			return None
		reader = getattr(store, "outbound_bodies_since", None) if body else None
		if reader is not None:
			from core.surfaces.user_delivery import content_hash
			h = content_hash(body)
			if not any(content_hash((b or "").strip()) == h for b in (reader(
					user_id or "", surface, owner_addr, cooldown) or [])):
				return None   # materially new — the owner has not seen this
		else:
			count = store.outbound_count_since(user_id or "", surface, owner_addr,
			                                   cooldown)
			if count <= 0:
				return None
	except Exception:
		logger.debug(
			"owner resend cooldown check failed (fail-open)", exc_info=True)
		return None
	_record_cooldown_suppression(event_log, user_id, surface, body)
	from tools.controller.types import ActionResult
	hours = cooldown / 3600
	repeat = " This exact text" if body else " An autonomous message"
	return ActionResult(
		extracted_content=(
			f"message:{repeat} already reached the owner on {surface} within "
			f"the last {hours:.1f}h. Call `contact_history` "
			f"(surface={surface!r}, address=<the owner address>) to see what "
			f"was already sent — say something materially new, or skip this "
			f"send entirely rather than repeating it."),
		include_in_memory=True)


def _record_cooldown_suppression(event_log, user_id: str, surface: str,
                                 body: str) -> None:
	"""Durable trace for a cooldown refusal, in the SAME two places every other
	suppression shape writes to, so `/missed` and telemetry can see it.

	Fail-open and silent: an unrecordable refusal is still a refusal, and this
	must never raise into the `message` action.
	"""
	try:
		from core.event_kinds import OWNER_NOTICE, USER_DELIVERY
		from core.surfaces.user_delivery import NOTICE_MARKERS, content_hash
		log = event_log
		if log is ...:
			from core.event_log import event_log_enabled, get_event_log
			log = get_event_log() if event_log_enabled() else None
		if log is None:
			return
		uid = str(user_id or "")
		if body:
			log.record(OWNER_NOTICE, user_id=uid, source="message_tool",
			           attrs={"text": f"{NOTICE_MARKERS[4]}; surface={surface}] "
			                          f"{body}"[:2000]})
		log.record(USER_DELIVERY, user_id=uid, source="message_tool",
		           attrs={"outcome": "cooldown", "lane": "normal",
		                  "content_hash": content_hash(body) if body else "",
		                  "text": body[:500]})
	except Exception:
		logger.debug("cooldown suppression record failed", exc_info=True)


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
		# 057 WS-E: the per-rail proof rule, rendered from core/rails/verification.py
		# and carried BY the result — so "did that land?" is answered here instead
		# of costing a turn to look up (and get wrong).
		proof = f"\n{res['verification']}" if res.get('verification') else ""
		return ActionResult(
			extracted_content=(
				f"message[{res['tier']}] -> {surface}:{target} OK{attached}{note} | text: {preview!r}{proof}"),
			include_in_memory=True)
	return ActionResult(
		extracted_content=(
			f"message[{res['tier']}] -> {surface}:{target} FAILED: {res.get('error') or ''}{note}"),
		error=res.get('error') or 'message send failed',
		include_in_memory=True)
