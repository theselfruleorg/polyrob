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
import re
from typing import Optional

from pydantic import BaseModel

from core.instance import OWNER_DOC_MAX_CHARS, SELF_DOC_MAX_CHARS
from core.runtime_paths import data_dir_or_home
from agents.task.loop_types.controller_types import ActionResult
from tools.controller._helpers import self_mod_emitter
from tools.controller.turn_origin import _is_forged_or_autonomous_turn

logger = logging.getLogger(__name__)


def _missing_reason(params, is_forged: bool) -> Optional[str]:
	"""096 D3: an owner-turn draft needs a reason — the owner decides from it."""
	if is_forged or (getattr(params, "reason", None) or "").strip():
		return None
	return ("a write needs `reason`: one sentence for the owner on why this change "
	        "(what the owner said, or what went wrong).")


def _record_doc_reason(writer, params, res, *, user_id: str, created_by: str,
                       data_dir) -> None:
	"""096 W1: store why, BEFORE the owner is notified. Fail-open."""
	reason = (getattr(params, "reason", None) or "").strip()
	if not (reason and getattr(res, "pending", False)):
		return
	try:
		from core import pending_reasons
		from core.instance import resolve_instance_id
		pending_reasons.record(user_id, writer._DOC_KIND, user_id, reason, by=created_by,
		                       home_dir=data_dir, instance_id=resolve_instance_id())
	except Exception as e:
		logger.debug("doc reason not recorded: %s", e)


def _doc_changes(writer, user_id: str) -> str:
	"""096 A9: the Changes line of the queued reply ("" when unreadable)."""
	try:
		from pathlib import Path
		from core.pending_review import change_summary, parts_text
		pend = writer.list_pending(user_id)
		if not pend:
			return ""
		body = Path(pend["path"]).read_text(encoding="utf-8")
		change = change_summary(body, writer.read_active(user_id) or None)
		return "a new document" if change["new"] else parts_text(change["parts"])
	except Exception:
		return ""


def _write_doc(writer, params, *, user_id: str, created_by: str):
	"""One write path for both identity docs: update|patch, always a draft.

	A model-written line is never in force before the owner reviews it: an
	owner conversation can carry untrusted tool results, so its origin is not
	proof that the owner wrote the line (095 W10 deleted the dead "apply now"
	branches).

	057 WS-D: `source`/`observed_at` ride through to the writer, which stamps the
	NEW or CHANGED lines ` [from: <source> <date>]`. Both are optional — with
	neither, the write is byte-identical to pre-057.
	"""
	prov = {"source": getattr(params, "source", None),
	        "observed_at": getattr(params, "observed_at", None)}
	if params.action == "update":
		if not params.content:
			return None, "update requires `content`."
		return writer.propose(params.content, user_id=user_id,
		                      created_by=created_by, pending=True, **prov), None
	if params.old_string is None or params.new_string is None:
		return None, "patch requires `old_string` and `new_string`."
	return writer.patch(user_id=user_id, old_string=params.old_string,
	                    new_string=params.new_string, replace_all=params.replace_all,
	                    created_by=created_by, pending=True, **prov), None


#: F4 (prod review 2026-10-29): above this share of the cap a write tells the
#: model to replace the rule a directive changes, not to append beside it.
_NEAR_CAP_RATIO = 0.9
NOTHING_WRITTEN = "Nothing was written. "
_REPLACE_HINT = ("When the owner's new directive changes a standing rule, REPLACE that "
                 "rule: action='replace_rule', rule_start=<its first words>, "
                 "new_string=<the new rule>. The old text moves under '## Superseded' "
                 "and stops counting toward the cap. Never append a second rule beside it.")
_RULE_PREFIX = re.compile(r"^[\s#>*_`-]+")


def _norm_rule(text: str) -> str:
	return " ".join(_RULE_PREFIX.sub("", text or "").lower().split())


_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


def _is_heading(line: str) -> bool:
	return line.lstrip().startswith("#")


def find_rule(editable: str, rule_start: str) -> list:
	"""The ACTIVE rule text(s) that ``rule_start`` names. Pure.

	A heading line is never part of a rule. In a paragraph that is a list, a rule
	is ONE item (the bullet and its indented continuation lines) — never the
	whole block, so the sibling rules stay where they are. A prose paragraph
	whose first body line starts with ``rule_start`` is one rule. Else any
	single non-heading line that does."""
	key = _norm_rule(rule_start)
	if not key:
		return []
	paragraphs = [p.strip("\n") for p in re.split(r"\n[ \t]*\n", editable or "") if p.strip()]
	hits = []
	for para in paragraphs:
		raw = para.split("\n")
		body = [i for i, ln in enumerate(raw) if ln.strip() and not _is_heading(ln)]
		if not body:
			continue
		if any(_LIST_ITEM.match(raw[i]) for i in body):
			for n, i in enumerate(body):
				if not (_LIST_ITEM.match(raw[i]) and _norm_rule(raw[i]).startswith(key)):
					continue
				end = i + 1
				while (end < len(raw) and raw[end].strip() and not _is_heading(raw[end])
				       and not _LIST_ITEM.match(raw[end]) and raw[end][:1] in (" ", "\t")):
					end += 1
				hits.append("\n".join(raw[i:end]))
		elif _norm_rule(raw[body[0]]).startswith(key):
			# A prose rule ends at the next heading: a doc with no blank lines must
			# never carry the following section into the replace.
			end = body[0] + 1
			while end < len(raw) and not _is_heading(raw[end]):
				end += 1
			hits.append("\n".join(raw[body[0]:end]).rstrip("\n"))
	if hits:
		return hits
	return [ln for para in paragraphs for ln in para.split("\n")
	        if ln.strip() and not _is_heading(ln) and _norm_rule(ln).startswith(key)]


def _draft_or_active(writer, user_id: str) -> str:
	"""The text a patch edits: the pending draft when one exists, else the active doc."""
	try:
		uid = writer._require_user(user_id)
		pending = writer._pending_file(uid) if uid else None
		if pending is not None and pending.is_file():
			return pending.read_text(encoding="utf-8")
	except Exception:
		pass
	return writer.read_active(user_id)


def _headroom_note(writer, res, doc_name: str, *, replace_hint: bool) -> str:
	"""``owner.md rules: 21,300/24,000 chars (2,700 left).`` after a write, and near
	the cap the instruction to replace instead of append. Fail-open to ""."""
	try:
		from pathlib import Path
		body = Path(res.path).read_text(encoding="utf-8") if getattr(res, "path", None) else ""
		used, cap = writer._capped_len(body), int(writer._MAX_CHARS)
		note = f"\n{doc_name}: {used:,}/{cap:,} chars ({max(cap - used, 0):,} left)."
		if used >= _NEAR_CAP_RATIO * cap:
			note += (" Near the cap. " + (_REPLACE_HINT if replace_hint else
			         "Replace or tighten an older note instead of appending a new one."))
		return note
	except Exception:
		return ""


def _rejected(label: str, errors, *, replace_hint: bool) -> str:
	text = f"{NOTHING_WRITTEN}{label} rejected: {'; '.join(errors)}"
	if replace_hint and any("chars" in str(e) and "/" in str(e) for e in errors or ()):
		text += " " + _REPLACE_HINT
	return text


def _identity_location(writer, user_id: str) -> str:
	"""Where the identity docs live (prod 2026-10-09: the agent told the owner it
	"cannot find an identity md" while owner.md/self.md sat in this directory)."""
	try:
		root = writer._root(user_id)
	except Exception:
		return ""
	return (f"[identity docs in {root}: owner.md = the owner's rules (this tool), "
	        f"self.md = your own notes (self_context_manage), contract.md = the operating "
	        f"contract. Change them only through these tools — a direct file edit skips "
	        f"the safety scan and the owner's review.]\n\n")


def _sweep_report(controller, rule: str, user_id: str, data_dir: str) -> str:
	"""060 WS-4: run the core sweep with the skill texts this tier can reach."""
	from core.instance import resolve_instance_id
	from core.runtime_paths import cron_db_path, goals_db_path
	from core.rules_sweep import render_sweep, sweep
	skills = None
	try:
		from agents.task.agent.skill_manager import get_skill_manager
		sm = get_skill_manager()
		skills = [(sid, sm._load_skill_content(sid, user_id=user_id) or "")
		          for sid in sm.get_skill_ids()]
	except Exception as e:
		logger.info("rules sweep: skills not swept (%s)", e)
	result = sweep(rule, user_id=user_id, data_dir=data_dir,
	               instance_id=resolve_instance_id(), cron_db=cron_db_path(data_dir),
	               goals_db=goals_db_path(data_dir), skills=skills)
	return render_sweep(result, rule)


def _sweep_new_rules(controller, before: str, after: str, user_id: str,
                     data_dir: str) -> str:
	"""After an owner-rule draft is written, name what its new lines would
	override (advisory; 095 W10 X-21: every owner.md write is swept). Empty when
	nothing contradicts it or the sweep cannot run — the write already
	succeeded, and a sweep fault must not read as a failed write."""
	try:
		from core.doc_claims import changed_lines, split_superseded, strip_stamp
		added = [strip_stamp(l).strip() for l in
		         changed_lines(split_superseded(before or "")[0], split_superseded(after or "")[0])]
		if not added:
			return ""
		report = _sweep_report(controller, "\n".join(added), user_id, data_dir)
		if "may contradict it" not in report:
			return ""
		return "\n\n" + report
	except Exception as e:
		logger.warning("rules sweep after an owner-rule write failed: %s", e)
		return ""


def _queued_text(label: str, kind: str, user_id: str, queue_depth: int = 0,
                 changes: str = "") -> str:
	"""035 P0-4 — "saved" read as DONE. On 09-08/09-09 the agent reported exactly
	that for two owner directives, which then sat inert in `.pending/` for two
	days while it kept doing the thing it had been told to stop.

	035 P1-8 asks IN BAND rather than relying on a proactive push the daily cap
	can drop: the reply carries the ask, and when the queue has grown it says so
	and offers the one command that clears it. A queue of four unreviewed
	proposals is what the incident actually looked like from the owner's side.

	⚠ 2026-09-21: the token printed here was `/approve owner_doc:<instance>`, which is
	NOT TAPPABLE. Telegram auto-links exactly one `/word` of `[A-Za-z0-9_]` and
	the argument is neither linked nor sent, so the only tappable half did
	nothing. On 09-20 the owner tapped three times over eight minutes and
	promoted nothing; the rule sat inert for 27 hours while the agent kept
	breaking it. The decision token is now rendered by the ONE SSOT every other
	owner surface uses (`self_evolution.pending_tap_token`), so what the owner
	taps and what the router resolves cannot drift. `/approve all` (a space) had
	the same defect and becomes `/approve_all`.
	"""
	try:
		from core.self_evolution import pending_tap_token
		_it = {"kind": kind, "id": user_id}
		tap = pending_tap_token("approve", _it)
		# 096 A9: the whole decision line — approve, reject AND read it.
		taps = (f"{tap} · {pending_tap_token('reject', _it)} · "
		        f"read it: {pending_tap_token('pending', _it)}")
	except Exception:  # fail-open: name the listing verb, never a dead token
		tap = taps = "/pending"
	out = (f"{label} draft saved but NOT YET IN EFFECT — it is queued for your "
	       f"review and changes nothing until you approve it.\n"
	       + (f"Changes: {changes}\n" if changes else "")
	       + f"To activate, tap: {taps}   "
	       f"(`/pending` lists everything waiting).\n"
	       f"Relay this tap line to the owner exactly as written.")
	if queue_depth > 1:
		out += (f"\n⚠ {queue_depth} proposals are now waiting on you — "
		        f"/approve_all decides every one of them.")
	return out


def skill_saved_text(skill_id: str, where: str) -> str:
	"""The `skill_manage` reply. 2026-10-14: "saved (pending review)" read as
	DONE — the agent told the owner a promo-bounty rule was "Fixed" while the
	patch sat inert in `.pending/`, so a pending save now says it is inert."""
	if where == "active":
		return f"Skill `{skill_id}` saved (active)."
	return (f"Skill `{skill_id}` is pending review. " + _queued_text("Skill", "skill", skill_id)
	        + "\nDo not tell the owner it is fixed or live until they approve it.")


#: The most owner.md lines one programme-term change may retire (the writer's
#: structure guard refuses more anyway).
_MAX_SUPERSEDE = 3


def draft_owner_supersede(controller, skill_id: str, new_text: str, *, user_id: str,
                          created_by: str, link: bool = True) -> str:
	"""095 W10 step 4 (R8 — supersede, do not overwrite). A programme-term change
	in a lane's pinned skill also drafts the retire of the owner.md line(s) on the
	same subject, and LINKS the two drafts: one owner tap promotes both or
	neither (``core.pending_links``). Only on an owner turn, only for a skill an
	enabled job or live goal pins, only when owner.md is writable here. The lines
	are found by the rules sweep's matcher (``rules_sweep.same_subject_lines``).
	Returns the sentence for the skill_manage result ("" when nothing was drafted).
	Fail-open: the skill draft stands either way."""
	try:
		from core.config_policy import AutonomyConfig
		if not AutonomyConfig.owner_doc_writable():
			return ""
		from core.doc_claims import split_superseded_raw
		from core.instance import resolve_instance_id
		from core.owner_doc_writer import OwnerDocWriter
		from core.runtime_paths import cron_db_path, goals_db_path
		from core.rules_sweep import pinned_by_a_rail, same_subject_lines
		_cfg = getattr(getattr(controller, 'container', None), 'config', None)
		data_dir = data_dir_or_home(getattr(_cfg, 'data_dir', None))
		if not pinned_by_a_rail(skill_id, user_id, cron_db_path(data_dir), goals_db_path(data_dir)):
			return ""
		instance_id = resolve_instance_id()
		writer = OwnerDocWriter(data_dir, instance_id=instance_id)
		editable = split_superseded_raw(_draft_or_active(writer, user_id))[0]
		rules = []
		for line in same_subject_lines(new_text, skill_id, editable):
			hits = find_rule(editable, line[:80])
			if len(hits) == 1 and hits[0] not in rules:
				rules.append(hits[0])
		retired = []
		for rule in rules[:_MAX_SUPERSEDE]:
			res = writer.patch(user_id=user_id, old_string=rule, new_string="",
			                   created_by=created_by, pending=True,
			                   source=f"superseded by skill {skill_id}")
			if res.ok:
				retired.append(rule.strip().split("\n")[0][:100])
		if not retired:
			return ""
		try:  # 096 W1: the retire half says why it exists
			from core import pending_reasons
			pending_reasons.record(
				user_id, "owner_doc", user_id,
				f"skill '{skill_id}' changes the same terms; this retires the older "
				f"owner rule so the two do not disagree", by=created_by,
				home_dir=data_dir, instance_id=instance_id)
		except Exception:
			pass
		text = ("\nAlso drafted: the retire of the owner.md line(s) on the same subject "
		        "(moved under '## Superseded' on approval): "
		        + " | ".join(f'"{r}"' for r in retired) + ".")
		if not link:  # the skill change is already active: the retire waits alone
			return text + " It is in /pending."
		from core import pending_links
		pending_links.link(user_id, ("skill", skill_id), ("owner_doc", user_id),
		                   home_dir=data_dir, instance_id=instance_id)
		return text + (" Both drafts are ONE decision: approving either promotes both, "
		               "rejecting either rejects both.")
	except Exception as e:
		logger.warning("owner.md supersede draft for skill %s failed: %s", skill_id, e)
		return ""


def _queue_depth(user_id: str, data_dir, instance_id: str) -> int:
	"""How many proposals are waiting on the owner (035 P1-8). Fail-open to 0."""
	try:
		from core.self_evolution import list_pending
		return len(list_pending(user_id, home_dir=data_dir, instance_id=instance_id))
	except Exception:
		return 0


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
			content: Optional[str] = None      # update: full self.md body (≤SELF_DOC_MAX_CHARS)
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
			# 096 W1/D3: why, one sentence for the owner (required on a write).
			reason: Optional[str] = None

		from core.prompt_text import owner_route

		@self.registry.action(
			owner_route("self_doc") + " YOUR OWN durable notes: lessons "
			"learned, conventions you follow, what worked and what did not. "
			"action='read' "
			f"returns the current text; action='update' replaces it (≤{SELF_DOC_MAX_CHARS} chars — "
			"consolidate, don't sprawl); action='patch' edits by exact-string replace; "
			"Every update/patch is a draft the owner reviews (/pending) and needs reason= "
			"(one sentence for the owner: why this change). This is NOT your core "
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
				return ActionResult(
					error="Promotion requires owner review through /pending; a model tool call cannot approve its draft.",
					include_in_memory=True)

			created_by = PROVENANCE_BACKGROUND if is_forged else PROVENANCE_AGENT
			_no_reason = _missing_reason(params, is_forged)
			if _no_reason:
				return ActionResult(error=NOTHING_WRITTEN + _no_reason, include_in_memory=True)
			try:
				res, err = _write_doc(writer, params, user_id=user_id, created_by=created_by)
				if err:
					return ActionResult(error=NOTHING_WRITTEN + err, include_in_memory=True)
			except Exception as e:
				self.logger.debug(f"self_context_manage failed: {e}")
				return ActionResult(error=f"{NOTHING_WRITTEN}self_context_manage failed: {e}",
				                    include_in_memory=True)

			if not res.ok:
				return ActionResult(error=_rejected("Self-context", res.errors, replace_hint=False),
				                    include_in_memory=True)
			_record_doc_reason(writer, params, res, user_id=user_id, created_by=created_by,
			                   data_dir=data_dir)
			_room = _headroom_note(writer, res, "self.md", replace_hint=False)
			_self_mod_ev(params.action, pending=res.pending, created_by=created_by)
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
					_queue_depth(user_id, data_dir, resolve_instance_id()),
					changes=_doc_changes(writer, user_id)) + _room,
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
			action: _Literal["update", "patch", "replace_rule", "read", "promote", "sweep"]
			content: Optional[str] = None      # update: full owner.md body (≤OWNER_DOC_MAX_CHARS)
			old_string: Optional[str] = None   # patch: exact text to replace
			new_string: Optional[str] = None   # patch / replace_rule: replacement
			replace_all: bool = False
			rule_start: Optional[str] = None   # replace_rule: the first words of ONE active rule
			# 057 WS-D — provenance. `source` = where this came from (free text:
			# 'room_read', 'owner said', 'measured'); `observed_at` = the ISO date
			# it was true (defaults to today). Given, they stamp every NEW or
			# CHANGED line ` [from: <source> <date>]` — and that stamp is part of
			# the body, so it counts toward the char cap.
			source: Optional[str] = None
			observed_at: Optional[str] = None
			# 096 W1/D3: why, one sentence for the owner (required on a write).
			reason: Optional[str] = None

		from core.prompt_text import owner_route

		@self.registry.action(
			"Record the OWNER's durable facts and STANDING RULES — timezone, projects, "
			"how they want you to operate (owner.md in your identity directory, "
			f"≤{OWNER_DOC_MAX_CHARS} chars). " + owner_route("owner_doc") + " "
			"action='read' returns it with its path — never search the disk for it. "
			"action='update' replaces the whole doc; action='patch' edits the ACTIVE "
			"rules by exact-string replace; action='replace_rule' (rule_start=<first "
			"words of ONE active rule>, new_string=<the new rule>, none = retire it) "
			"replaces that rule in place; the old text moves under '## Superseded' (off "
			"the cap). Every write needs reason= (one sentence for the owner: why this "
			"change) and reports the chars left. "
			"Pass source= ('owner said', 'measured') and observed_at=YYYY-MM-DD to stamp "
			"new lines (counts toward the cap); a line CLAIMING a world fact (broken, "
			"since …) may be refused without a source. action='sweep' content=<a rule> "
			"lists standing instructions (owner rules, SOUL, skills, job/goal text, "
			"`kind: instruction` docs) that seem to contradict it; advisory; every write "
			"is swept too.",
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
					extracted_content=(_identity_location(writer, user_id)
					                   + (body or "(no owner-facts doc yet)")),
					include_in_memory=True,
				)

			if params.action == "sweep":
				if not (params.content or "").strip():
					return ActionResult(error="sweep requires `content` (the rule to sweep for).",
					                    include_in_memory=True)
				return ActionResult(
					extracted_content=_sweep_report(self, params.content, user_id, data_dir),
					include_in_memory=True)

			is_forged = _is_forged_or_autonomous_turn(execution_context, self)

			if params.action == "promote":
				return ActionResult(
					error="Promotion requires owner review through /pending; a model tool call cannot approve its draft.",
					include_in_memory=True)

			if params.action == "replace_rule":
				# F4: one rule, found by its first words, through the patch path —
				# so the old text is superseded (dated, kept) and leaves the cap.
				from core.doc_claims import split_superseded_raw
				editable = split_superseded_raw(_draft_or_active(writer, user_id))[0]
				hits = find_rule(editable, params.rule_start or "")
				if len(hits) != 1:
					why = (f"no active rule starts with {params.rule_start!r} — "
					       "action='read' shows the rules" if not hits else
					       f"{len(hits)} active rules start with {params.rule_start!r} — "
					       "give more of the first words: "
					       + " | ".join(h.split(chr(10))[0][:60] for h in hits[:4]))
					return ActionResult(error=NOTHING_WRITTEN + why, include_in_memory=True)
				_new = (params.new_string or "").strip()
				_bullet = _LIST_ITEM.match(hits[0])
				if _new and _bullet and not _LIST_ITEM.match(_new):
					_new = _bullet.group(0) + _new   # a list item stays a list item
				params = params.model_copy(update={
					"action": "patch", "old_string": hits[0],
					"new_string": _new, "replace_all": False})

			created_by = PROVENANCE_BACKGROUND if is_forged else PROVENANCE_AGENT
			_no_reason = _missing_reason(params, is_forged)
			if _no_reason:
				return ActionResult(error=NOTHING_WRITTEN + _no_reason, include_in_memory=True)
			try:
				res, err = _write_doc(writer, params, user_id=user_id, created_by=created_by)
				if err:
					return ActionResult(error=NOTHING_WRITTEN + err, include_in_memory=True)
			except Exception as e:
				self.logger.debug(f"owner_doc_manage failed: {e}")
				return ActionResult(error=f"{NOTHING_WRITTEN}owner_doc_manage failed: {e}",
				                    include_in_memory=True)

			if not res.ok:
				return ActionResult(error=_rejected("Owner-facts doc", res.errors, replace_hint=True),
				                    include_in_memory=True)
			_record_doc_reason(writer, params, res, user_id=user_id, created_by=created_by,
			                   data_dir=data_dir)
			_room = _headroom_note(writer, res, "owner.md rules", replace_hint=True)
			_self_mod_ev(params.action, pending=res.pending, created_by=created_by)
			_swept = _sweep_new_rules(self, writer.read_active(user_id),
			                          _draft_or_active(writer, user_id), user_id, data_dir)
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
					_queue_depth(user_id, data_dir, resolve_instance_id()),
					changes=_doc_changes(writer, user_id)) + _room + _swept,
				include_in_memory=True,
			)
