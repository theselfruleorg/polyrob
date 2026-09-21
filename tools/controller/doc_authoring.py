"""DocAuthoringMixin — the two agent-callable document-authoring actions.

`self_context_manage` (the evolving SELF doc) and `owner_doc_manage` (the
owner-facts doc) are one concern: the agent writing its own durable, per-tenant
identity documents, each behind its own flag, each with a `.pending` review lane
and a forged-turn refusal. Extracted from `action_registration.py` 2026-09-08 to
bring that file back under its shrink-only size ratchet ceiling; the code is
verbatim code-motion and `Controller` composes this mixin, so `self` is the same
composed Controller via the MRO and every closure behaves identically.

⚠️ NO ``from __future__ import annotations`` in this module. The Registry
introspects each action closure's first-param annotation to route the validated
param model (`registry/service.py`, `issubclass(first_anno, BaseModel)`);
stringizing those annotations silently breaks that routing. Same rule as
`action_registration.py` — see AGENTS.md.
"""
import logging
from typing import Optional

from pydantic import BaseModel

from core.runtime_paths import data_dir_or_home
from tools.controller.types import ActionResult
from tools.controller._helpers import self_mod_emitter
from tools.controller.turn_origin import _is_forged_or_autonomous_turn

logger = logging.getLogger(__name__)


def _rules_immediate(is_forged: bool) -> bool:
	"""May this turn's document write bind IMMEDIATELY? (035 P1-6)

	True only when the flag is on AND the turn is a genuine owner turn. The
	writer enforces the forged-author refusal independently
	(`SelfContextWriter._resolve_pending`), so this is the CALLER half of a
	two-sided guard, never the only one.
	"""
	if is_forged:
		return False
	try:
		from core.config_policy import AutonomyConfig
		return bool(AutonomyConfig.owner_rules_immediate())
	except Exception:
		# Fail CLOSED: an unreadable flag keeps today's review lane.
		return False


def _write_doc(writer, params, *, user_id: str, created_by: str, immediate: bool):
	"""One write path for both identity docs: update|patch, immediate or queued.

	057 WS-D: `source`/`observed_at` ride through to the writer, which stamps the
	NEW or CHANGED lines ` [from: <source> <date>]`. Both are optional — with
	neither, the write is byte-identical to pre-057 unless
	`DOC_CLAIM_PROVENANCE_REQUIRED` is armed.
	"""
	prov = {"source": getattr(params, "source", None),
	        "observed_at": getattr(params, "observed_at", None)}
	if params.action == "update":
		if not params.content:
			return None, "update requires `content`."
		if immediate:
			return writer.apply_now(params.content, user_id=user_id,
			                        created_by=created_by, **prov), None
		return writer.propose(params.content, user_id=user_id,
		                      created_by=created_by, pending=True, **prov), None
	if params.old_string is None or params.new_string is None:
		return None, "patch requires `old_string` and `new_string`."
	res = writer.patch(user_id=user_id, old_string=params.old_string,
	                   new_string=params.new_string, replace_all=params.replace_all,
	                   created_by=created_by, pending=not immediate, **prov)
	if immediate and res.ok and not res.pending:
		# same honesty rule as apply_now: a superseded draft must leave the queue
		writer.retire_pending(user_id=user_id)
	return res, None


def _applied_now_text(label: str, summary: str) -> str:
	"""035 P1-11 — report-after. The owner asked for this on THIS turn, so the
	report belongs in the reply, not in another proactive push competing for the
	daily cap."""
	return (f"{label} IS IN EFFECT NOW (applied directly — you asked for it on this "
	        f"turn, so it needed no approval step).\nWhat changed: {summary}")


def _queued_text(label: str, kind: str, user_id: str, queue_depth: int = 0) -> str:
	"""035 P0-4 — "saved" read as DONE. On 09-08/09-09 the agent reported exactly
	that for two owner directives, which then sat inert in `.pending/` for two
	days while it kept doing the thing it had been told to stop.

	035 P1-8 asks IN BAND rather than relying on a proactive push the daily cap
	can drop: the reply carries the ask, and when the queue has grown it says so
	and offers the one command that clears it. A queue of four unreviewed
	proposals is what the incident actually looked like from the owner's side.
	"""
	out = (f"{label} draft saved but NOT YET IN EFFECT — it is queued for your "
	       f"review and changes nothing until you approve it.\n"
	       f"To activate: /approve {kind}:{user_id}   "
	       f"(or `polyrob owner promote {kind} {user_id}`; "
	       f"`/pending` lists everything waiting).")
	if queue_depth > 1:
		out += (f"\n⚠ {queue_depth} proposals are now waiting on you — "
		        f"`/approve all` decides every one of them.")
	return out


def _queue_depth(user_id: str, data_dir, instance_id: str) -> int:
	"""How many proposals are waiting on the owner (035 P1-8). Fail-open to 0."""
	try:
		from core.self_evolution import list_pending
		return len(list_pending(user_id, home_dir=data_dir, instance_id=instance_id))
	except Exception:
		return 0


def perform_rules_propose(text: str, *, user_id: str, data_dir, instance_id: str,
                          is_forged: bool):
	"""Record a durable operating RULE in the owner RULES doc. Returns an
	``ActionResult``.

	035 P1-7: `contract.md` had a real reader (`load_contract_doc`,
	CONTRACT_DOC_ENABLED default ON) and, over the whole measured life of the
	subsystem on prod, ZERO writes — a dead lane behind live plumbing, costing a
	document kind, a writer and a tool-description slot. An owner-authored prose
	rule and an "operating contract" are the same thing, so
	`preferences(contract_propose)` now routes here and writes `owner.md`, which
	also means it inherits P1-6: on a genuine owner turn the rule BINDS
	immediately instead of queueing.

	No NEW contract draft is ever created. An EXISTING one stays listable and
	promotable via `/pending` for one release (035 §6 migration step 2); the
	physical removal of ContractWriter/KIND_CONTRACT lands after that, so this
	release cannot orphan a queued proposal.

	Lives HERE, not in `action_registration.py`: that file is a god-file under a
	shrink-only size ratchet, and this is the same concern its sibling
	doc-authoring write paths already implement.
	"""
	from core.owner_doc_writer import OwnerDocWriter
	from core.self_context_writer import PROVENANCE_AGENT, PROVENANCE_BACKGROUND
	created_by = PROVENANCE_BACKGROUND if is_forged else PROVENANCE_AGENT
	writer = OwnerDocWriter(data_dir, instance_id=instance_id)
	immediate = _rules_immediate(is_forged)
	before = writer.read_active(user_id) if immediate else ""
	if immediate:
		res = writer.apply_now(text, user_id=user_id, created_by=created_by)
	else:
		res = writer.propose(text, user_id=user_id, created_by=created_by,
		                     pending=True)
	if not res.ok:
		return res, ActionResult(
			error=f"Operating rule rejected: {'; '.join(res.errors)}",
			include_in_memory=True)
	if not res.pending:
		from core.self_evolution import summarize_doc_change
		return res, ActionResult(
			extracted_content=_applied_now_text(
				"Operating rule", summarize_doc_change(
					before, writer.read_active(user_id))),
			include_in_memory=True)
	return res, ActionResult(
		extracted_content=_queued_text("Operating rule", "owner_doc", user_id,
		                               _queue_depth(user_id, data_dir, instance_id)),
		include_in_memory=True)


class DocAuthoringMixin:
	"""`self_context_manage` + `owner_doc_manage` registration."""

	def _register_self_context_manage_action(self):
		"""Register the evolving SELF-identity tool `self_context_manage`, gated
		SELF_CONTEXT_WRITABLE (default OFF; ON under POLYROB_LOCAL).

		Lets the agent refine its own per-(instance,user) ``self.md`` — the learned
		"how I work with this user" layer. Safety lives in SelfContextWriter:
		tenant-confined + anon-blocked, identity-scanned fail-CLOSED (self-voice
		subversion + invisible-unicode), over-cap ERRORS (forces consolidation),
		forged turns forced to .pending and barred from active docs, atomic write,
		archive-never-delete. The SOUL tier (identity.md/operating.md) is NEVER
		reachable here — it stays operator-only. Writes apply NEXT session (the
		foundation snapshot is frozen at session start). Registered in this module —
		no `from __future__ import annotations` (registry-closure introspection)."""
		try:
			from core.config_policy import AutonomyConfig
			if not AutonomyConfig.self_context_writable():
				return
		except Exception:
			return

		from typing import Literal as _Literal

		class SelfContextManageAction(BaseModel):
			action: _Literal["update", "patch", "read", "promote"]
			content: Optional[str] = None      # update: full self.md body (≤2200 chars)
			old_string: Optional[str] = None   # patch: exact text to replace
			new_string: Optional[str] = None   # patch: replacement
			replace_all: bool = False
			# 057 WS-D — provenance. `source` = where this came from (free text:
			# 'room_read', 'owner said', 'measured'); `observed_at` = the ISO date
			# it was true (defaults to today). Given, they stamp every NEW or
			# CHANGED line ` [from: <source> <date>]` — and that stamp is part of
			# the body, so it counts toward the char cap.
			source: Optional[str] = None
			observed_at: Optional[str] = None

		@self.registry.action(
			"Refine your evolving SELF context — YOUR OWN durable notes: lessons "
			"learned, conventions you follow, what worked and what did not. NOT the "
			"place for an owner instruction (use `owner_doc_manage`) and NOT for a "
			"typed setting like reply length or tone (use `preferences`). "
			"action='read' "
			"returns the current text; action='update' replaces it (≤2200 chars — "
			"consolidate, don't sprawl); action='patch' edits by exact-string replace; "
			"action='promote' activates your pending draft (owner-only). Updates/patches "
			"are QUARANTINED for review and apply next session. This is NOT your core "
			"identity/boundaries (those are operator-owned). "
			"Pass source= (where it came from: 'measured', 'owner said', 'room_read') "
			"and observed_at=YYYY-MM-DD (defaults to today) so each new or changed line "
			"is written with its provenance; that stamp counts toward the char cap.",
			param_model=SelfContextManageAction,
		)
		async def self_context_manage(params: SelfContextManageAction, execution_context=None) -> ActionResult:
			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
			if not user_id:
				return ActionResult(error="self-context requires a user (tenant scope).",
				                    include_in_memory=True)

			# T4-06: every effected self-context mutation records a first-class
			# self_modification event (durable log → /telemetry + /activity). Fail-open.
			_self_mod_ev = self_mod_emitter(
				execution_context, self, user_id,
				kind="self_context", source="self_context_manage",
				item_id=user_id, created_by="")
			# Resolve the instance home dir (same as construction).
			_cfg = getattr(getattr(self, 'container', None), 'config', None)
			data_dir = data_dir_or_home(getattr(_cfg, 'data_dir', None))
			try:
				from core.instance import resolve_instance_id
				from core.self_context_writer import (
					SelfContextWriter, PROVENANCE_AGENT, PROVENANCE_BACKGROUND,
				)
				writer = SelfContextWriter(data_dir, instance_id=resolve_instance_id())
			except Exception as e:
				self.logger.debug(f"self_context_manage init failed: {e}")
				return ActionResult(error=f"self_context_manage unavailable: {e}", include_in_memory=True)

			if params.action == "read":
				body = writer.read(user_id)
				# Apply the same load-side [BLOCKED] guard as session-start injection
				# (load_self_doc) so a direct-FS-poisoned doc is never returned raw to
				# the model mid-session. Fail-closed on a missing/raising scanner.
				if body:
					try:
						from modules.memory.task.threat_scan import is_identity_suspicious
						if is_identity_suspicious(body):
							body = "[BLOCKED: self-context failed the identity safety scan]"
					except Exception:
						body = "[BLOCKED: identity scanner unavailable]"
				return ActionResult(
					extracted_content=(body or "(no self-context yet)"),
					include_in_memory=True,
				)

			# Forged/autonomous detection (C7): a sub-agent/leaf OR an autonomous
			# goal/cron/planner-spawned run must never promote its own pending
			# self-context (autonomous top-level runs are owner_ok under POLYROB_LOCAL).
			is_forged = _is_forged_or_autonomous_turn(execution_context, self)

			if params.action == "promote":
				# Activation is OWNER-only (Phase D). The caller is the owner when this
				# is the single-user local CLI OR their user_id matches the bound owner
				# principal (POLYROB_OWNER_USER_ID / first SURFACE_SUPER_ADMIN_USER_IDS).
				# A non-owner or any forged turn can never self-promote — that is what
				# keeps a self-wake / injected / sub-agent turn from activating its own
				# pending identity, on the server as well as locally.
				try:
					from core.config_policy import local_mode_enabled
					from core.instance import is_owner_local_safe, resolve_owner_principal
					# The local bypass is honored ONLY for the genuine single-user local
					# operator tenant — NOT any uid under the global POLYROB_LOCAL flag.
					# This action runs inside a session and has no surface id, so it can't
					# use the _LOCAL_OWNER_SURFACES filter that access.py/pairing.py apply;
					# is_owner_local_safe is the surface-independent equivalent (a forgeable
					# network sender's uid is never the local tenant). See permissions audit F4.
					owner_ok = is_owner_local_safe(
						user_id, owner_principal=resolve_owner_principal(),
						local_enabled=local_mode_enabled())
				except Exception:
					owner_ok = False
				if is_forged or not owner_ok:
					return ActionResult(
						error="promote is owner-only; your pending self-context awaits operator review.",
						include_in_memory=True)
				res = writer.promote(user_id=user_id)
				if not res.ok:
					return ActionResult(error=f"Promote failed: {'; '.join(res.errors)}",
					                    include_in_memory=True)
				_self_mod_ev("promote", pending=False, created_by="owner")
				return ActionResult(extracted_content="Self-context promoted (active next session).",
				                    include_in_memory=True)

			# update / patch. `created_by` reflects real forged status, so the writer
			# bars a forged turn from reading/patching an active doc and ALWAYS
			# quarantines it — independently of the 035 P1-6 decision below.
			created_by = PROVENANCE_BACKGROUND if is_forged else PROVENANCE_AGENT
			immediate = _rules_immediate(is_forged)
			before = writer.read_active(user_id) if immediate else ""
			try:
				res, err = _write_doc(writer, params, user_id=user_id,
				                      created_by=created_by, immediate=immediate)
				if err:
					return ActionResult(error=err, include_in_memory=True)
			except Exception as e:
				self.logger.debug(f"self_context_manage failed: {e}")
				return ActionResult(error=f"self_context_manage failed: {e}", include_in_memory=True)

			if not res.ok:
				return ActionResult(error=f"Self-context rejected: {'; '.join(res.errors)}",
				                    include_in_memory=True)
			_self_mod_ev(params.action, pending=res.pending, created_by=created_by)
			if not res.pending:
				from core.self_evolution import summarize_doc_change
				return ActionResult(
					extracted_content=_applied_now_text(
						"Self-context", summarize_doc_change(before, writer.read_active(user_id))),
					include_in_memory=True,
				)
			# §7.1: proactively tell the owner a proposal is waiting (fail-open,
			# gated SELF_EVOLUTION_TRANSPARENCY). Closes the "owner never told" gap.
			try:
				from core import self_evolution as _se
				await _se.maybe_notify_owner_pending(
					getattr(self, 'container', None), user_id,
					home_dir=data_dir, instance_id=resolve_instance_id())
			except Exception as _e:
				self.logger.debug(f"self-evolution notify skipped: {_e}")
			return ActionResult(
				extracted_content=_queued_text(
					"Self-context", "self_context", user_id,
					_queue_depth(user_id, data_dir, resolve_instance_id())),
				include_in_memory=True,
			)

	def _register_owner_doc_manage_action(self):
		"""Register the bounded owner-facts tool `owner_doc_manage`, gated
		OWNER_DOC_WRITABLE (default OFF; ON under POLYROB_LOCAL).

		Lets the agent maintain a small per-(instance,user) ``owner.md`` — durable
		facts/preferences about the OWNER, injected each session alongside SOUL/SELF.
		Same safety as self-context (OwnerDocWriter): tenant-confined + anon-blocked,
		identity-scanned fail-CLOSED, over-cap ERRORS, forged turns forced .pending
		and barred from active docs, atomic write, archive-never-delete. Writes apply
		NEXT session. No `from __future__ import annotations` (registry-closure
		introspection)."""
		try:
			from core.config_policy import AutonomyConfig
			if not AutonomyConfig.owner_doc_writable():
				return
		except Exception:
			return

		from typing import Literal as _Literal

		class OwnerDocManageAction(BaseModel):
			action: _Literal["update", "patch", "read", "promote"]
			content: Optional[str] = None      # update: full owner.md body (≤1600 chars)
			old_string: Optional[str] = None   # patch: exact text to replace
			new_string: Optional[str] = None   # patch: replacement
			replace_all: bool = False
			# 057 WS-D — provenance. `source` = where this came from (free text:
			# 'room_read', 'owner said', 'measured'); `observed_at` = the ISO date
			# it was true (defaults to today). Given, they stamp every NEW or
			# CHANGED line ` [from: <source> <date>]` — and that stamp is part of
			# the body, so it counts toward the char cap.
			source: Optional[str] = None
			observed_at: Optional[str] = None

		@self.registry.action(
			"Record the OWNER's durable facts and STANDING RULES — their timezone, "
			"projects, and any instruction they give you about how to operate "
			"(\"never post to X\", \"always do Y\", \"stop Z\"). This is where an owner "
			"directive belongs, so it survives the turn. For a TYPED setting "
			"(reply length, tone, digest, quotas, caps) use `preferences` instead — "
			"it applies immediately and is actually enforced (a small owner.md, "
			"≤1600 chars). "
			"action='read' returns it; action='update' replaces it (consolidate, keep "
			"only durable facts); action='patch' edits by exact-string replace; "
			"action='promote' activates your pending draft (owner-only). Updates/patches "
			"are QUARANTINED for review and apply next session. "
			"Pass source= (where the fact came from: 'owner said', 'measured', "
			"'room_read') and observed_at=YYYY-MM-DD (defaults to today) so each new or "
			"changed line is written with its provenance; that stamp counts toward the "
			"char cap. A line that CLAIMS something about the world (broken, blocked, "
			"disabled, lacks permission, since …) may be refused without a source.",
			param_model=OwnerDocManageAction,
		)
		async def owner_doc_manage(params: OwnerDocManageAction, execution_context=None) -> ActionResult:
			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
			if not user_id:
				return ActionResult(error="owner-facts doc requires a user (tenant scope).",
				                    include_in_memory=True)

			_self_mod_ev = self_mod_emitter(
				execution_context, self, user_id,
				kind="owner_doc", source="owner_doc_manage",
				item_id=user_id, created_by="")

			_cfg = getattr(getattr(self, 'container', None), 'config', None)
			data_dir = data_dir_or_home(getattr(_cfg, 'data_dir', None))
			try:
				from core.instance import resolve_instance_id
				from core.owner_doc_writer import (
					OwnerDocWriter, PROVENANCE_AGENT, PROVENANCE_BACKGROUND,
				)
				writer = OwnerDocWriter(data_dir, instance_id=resolve_instance_id())
			except Exception as e:
				self.logger.debug(f"owner_doc_manage init failed: {e}")
				return ActionResult(error=f"owner_doc_manage unavailable: {e}", include_in_memory=True)

			if params.action == "read":
				body = writer.read(user_id)
				if body:
					try:
						from modules.memory.task.threat_scan import is_identity_suspicious
						if is_identity_suspicious(body):
							body = "[BLOCKED: owner-facts doc failed the identity safety scan]"
					except Exception:
						body = "[BLOCKED: identity scanner unavailable]"
				return ActionResult(
					extracted_content=(body or "(no owner-facts doc yet)"),
					include_in_memory=True,
				)

			is_forged = _is_forged_or_autonomous_turn(execution_context, self)

			if params.action == "promote":
				try:
					from core.config_policy import local_mode_enabled
					from core.instance import is_owner_local_safe, resolve_owner_principal
					owner_ok = is_owner_local_safe(
						user_id, owner_principal=resolve_owner_principal(),
						local_enabled=local_mode_enabled())
				except Exception:
					owner_ok = False
				if is_forged or not owner_ok:
					return ActionResult(
						error="promote is owner-only; your pending owner-facts doc awaits operator review.",
						include_in_memory=True)
				res = writer.promote(user_id=user_id)
				if not res.ok:
					return ActionResult(error=f"Promote failed: {'; '.join(res.errors)}",
					                    include_in_memory=True)
				_self_mod_ev("promote", pending=False, created_by="owner")
				return ActionResult(extracted_content="Owner-facts doc promoted (active next session).",
				                    include_in_memory=True)

			created_by = PROVENANCE_BACKGROUND if is_forged else PROVENANCE_AGENT
			immediate = _rules_immediate(is_forged)
			before = writer.read_active(user_id) if immediate else ""
			try:
				res, err = _write_doc(writer, params, user_id=user_id,
				                      created_by=created_by, immediate=immediate)
				if err:
					return ActionResult(error=err, include_in_memory=True)
			except Exception as e:
				self.logger.debug(f"owner_doc_manage failed: {e}")
				return ActionResult(error=f"owner_doc_manage failed: {e}", include_in_memory=True)

			if not res.ok:
				return ActionResult(error=f"Owner-facts doc rejected: {'; '.join(res.errors)}",
				                    include_in_memory=True)
			_self_mod_ev(params.action, pending=res.pending, created_by=created_by)
			if not res.pending:
				from core.self_evolution import summarize_doc_change
				return ActionResult(
					extracted_content=_applied_now_text(
						"Owner-facts doc", summarize_doc_change(before, writer.read_active(user_id))),
					include_in_memory=True,
				)
			try:
				from core import self_evolution as _se
				await _se.maybe_notify_owner_pending(
					getattr(self, 'container', None), user_id,
					home_dir=data_dir, instance_id=resolve_instance_id())
			except Exception as _e:
				self.logger.debug(f"self-evolution notify skipped: {_e}")
			return ActionResult(
				extracted_content=_queued_text(
					"Owner-facts", "owner_doc", user_id,
					_queue_depth(user_id, data_dir, resolve_instance_id())),
				include_in_memory=True,
			)
