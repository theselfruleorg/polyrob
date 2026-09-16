"""Emit-side glue for the user-facing actions (`send_message` / `done`).

Extracted from ``action_registration.py`` when the communication contract landed:
that file is a god-file under a shrink-only size ratchet, and the repo rule is to
put NEW behaviour in a new module rather than grow it. Everything here answers one
question — "what do we tell the AGENT about the message it just sent?" — so it is
a real seam, not a size dodge.

Two concerns:

* :func:`describe_route_outcome` — was the text actually delivered, and if not, why.
* :func:`publish_context` — the session facts the outbound mirror needs to turn a
  workspace path into an address (attach / console URL / honest server-only line).
* :func:`attachment_receipt` — what became of the files the message named.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Outcomes from core.surfaces.user_delivery.deliver_user_message (via
# maybe_deliver_autonomous_send) that mean the text did NOT reach the user
# live, so send_message's own report must say so instead of a blanket
# "sent" — a resumed/recreated session (e.g. `polyrob run --resume`) has no
# other delivery path, and a blind "success" here is what let a genuinely
# undelivered owner reply go unnoticed on 2026-08-28 (fixed alongside this).
ROUTE_OUTCOME_NOT_DELIVERED = {
	"capped": "the owner's daily message cap was already reached",
	"rate_limited": "the owner's hourly rate limit was already reached",
	"deduped": "an identical message was already sent recently",
	"quiet_held": "quiet hours are in effect",
	"failed": "the delivery attempt raised an error",
	"empty": "the message text was empty",
}


def _reply_anchor(orchestrator, reply_to: Optional[str]) -> dict:
	"""``{"reply_to": …, "replied_ids": […]}`` for one publish (044 T10 + T20).

	``reply_to`` is the line this message answers: the caller's explicit choice
	first (a room SERVICE run picks its own lines and has no triggering line to
	inherit from), else the live room turn's anchor.

	``replied_ids`` is the RUN's own list of ledger ids it answered, lazily
	created on the orchestrator. The mirror appends to it — only for a ROOM key
	with an anchor — so ``finish_service_run`` can mark exactly those lines.
	"""
	replied_ids = getattr(orchestrator, "_room_replied_ids", None)
	if replied_ids is None and orchestrator is not None:
		replied_ids = []
		try:
			orchestrator._room_replied_ids = replied_ids
		except Exception:  # a frozen/odd orchestrator: record nothing, send anyway
			replied_ids = None
	return {"reply_to": reply_to or getattr(orchestrator, "_turn_reply_to", None),
			"replied_ids": replied_ids}


def publish_context(controller, reply_to: Optional[str] = None) -> dict:
	"""Session context for the outbound mirrors so C4 can turn a workspace path
	into an address (attach / console URL / honest server-only line).

	Resolved per call because the surface's media capability and the session
	workspace are both runtime facts. Fail-open to the legacy shape ({} =>
	no path resolution at all), so a lookup fault costs a link, never a message.

	``reply_to`` (044 T20) is the anchor the CALLER chose — ``send_message``'s own
	``reply_to`` parameter. A room SERVICE run reads a tail and picks which lines
	to answer, so it has no triggering line to inherit an anchor from; an explicit
	one therefore WINS over ``orchestrator._turn_reply_to`` (the live room turn's
	anchor, T10), which stays the fallback so a live turn is unchanged.
	"""
	try:
		orch = getattr(controller, "orchestrator", None)
		session_id = getattr(controller, "session_id", None)
		user_id = getattr(controller, "user_id", None) or getattr(orch, "user_id", None)
		anchor = _reply_anchor(orch, reply_to)
		from tools.controller.message_send import (
			_resolve_session_workspace, _surface_media_out,
		)
		workspace_dir = _resolve_session_workspace(session_id, user_id)
		if not workspace_dir:
			# No workspace => no PATH resolution. The reply anchor is not a path
			# concern, and losing it would unthread a room reply (and, worse, lose
			# the record of which ledger line the run answered).
			return anchor
		router = getattr(orch, "_message_router", None)
		# The bound surface id lives in the session key ("telegram:123").
		key = getattr(orch, "_chat_session_key", None) or ""
		surface_id = key.split(":", 1)[0] if ":" in key else key
		try:
			# Layering: core/ never imports modules.*, so the injection scanner is
			# supplied from this (tools) tier — the same contract
			# screen_attachment_path(scanner=) already requires.
			from modules.memory.task.threat_scan import is_suspicious as _raw_scanner
		except ImportError:
			_raw_scanner = None
		if _raw_scanner is not None:
			# Wrapped so a hit is visible wherever this scanner is actually invoked
			# downstream (core/surfaces/path_links.py) — the verdict itself (the
			# return value) is unchanged, only its visibility.
			# 045 I5: origin is "file" — this scanner screens a workspace FILE on
			# its way out, never an inbound turn. "sender" sent an owner hunting
			# for a hostile chat message that does not exist. And the report
			# carries the TENANT: a row with no user_id is readable by no seat.
			def scanner(text, _raw_scanner=_raw_scanner,
						_uid=user_id, _sid=session_id):
				hit = _raw_scanner(text)
				if hit:
					from core.security.threat_report import report_threat
					report_threat("file", source="controller_emit",
								  user_id=_uid or "", session_id=_sid or "")
				return hit
		else:
			scanner = None
		return {
			"session_id": session_id,
			"workspace_dir": workspace_dir,
			"media_ok": _surface_media_out(router, surface_id),
			"scanner": scanner,
			**anchor,
		}
	except Exception:
		logger.debug("publish context unresolved (fail-open)", exc_info=True)
		return {}


def describe_route_outcome(route_outcome: Optional[str]) -> Optional[str]:
	"""Honest suffix for send_message's ActionResult, or None to change nothing.

	``None``/``"sent"`` mean either a live mirror already handled delivery or
	the fallback rail genuinely delivered — the default "sent" wording stays
	accurate. Anything else means the text was NOT delivered to the user this
	way; say so rather than reporting a blanket success.
	"""
	if route_outcome in (None, "sent"):
		return None
	if route_outcome == "fallback":
		return ("no live delivery channel — queued as a durable owner notice "
				"(not an instant push; check `polyrob owner` on the next contact)")
	reason = ROUTE_OUTCOME_NOT_DELIVERED.get(
		route_outcome, f"outcome={route_outcome}")
	return f"NOT delivered to the user — {reason}"


def attachment_receipt(resolution) -> Optional[str]:
	"""One honest line naming what happened to the files this message referenced.

	Appended to the ACTION RESULT, never to the user's message. Without it the
	agent is attachment-blind: on 2026-07-19 that blindness had it resend one file
	~12 times and then wrongly conclude `media_paths` was unsupported. Fail-open to
	None — a receipt is feedback, never a reason to fail a delivered send.
	"""
	if not resolution:
		return None
	try:
		from core.surfaces.path_links import receipt
		return receipt(resolution)
	except Exception:
		logger.debug("attachment receipt unavailable (fail-open)", exc_info=True)
		return None


#: Back-compat aliases: ``action_registration`` re-exports these under their old
#: private names, and a test imports ``_describe_route_outcome`` from there.
_ROUTE_OUTCOME_NOT_DELIVERED = ROUTE_OUTCOME_NOT_DELIVERED
_describe_route_outcome = describe_route_outcome
_publish_context = publish_context
