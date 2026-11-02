"""PreferencesActionMixin — the agent-callable `preferences` action.

Extracted from `action_registration.py` (proposal 095 P0.2) to bring that file
back under its shrink-only size ratchet ceiling. The code is verbatim
code-motion and `Controller` composes this mixin through
`ActionRegistrationMixin`, so `self` is the same composed Controller via the
MRO and the closure behaves identically.

⚠️ NO ``from __future__ import annotations`` in this module. The Registry
introspects each action closure's first-param annotation to route the validated
param model (`registry/service.py`, `issubclass(first_anno, BaseModel)`);
stringizing those annotations silently breaks that routing. Same rule as
`action_registration.py` — see AGENTS.md.
"""
import logging
from typing import Optional

from pydantic import BaseModel, Field

from core.runtime_paths import prefs_home_dir
from agents.task.loop_types.controller_types import ActionResult
from tools.controller._helpers import self_mod_emitter
from tools.controller.turn_origin import _is_forged_or_autonomous_turn

logger = logging.getLogger(__name__)

_MASKED = "(set, masked)"   # core.config_service already hid a secret flag


def _shown(name, value):
	"""095 W1c: a credential-shaped value (a secret key, or text the secret
	scrubber would change) is shown only as its fingerprint."""
	text = "" if value is None else str(value)
	if not text or text == _MASKED:
		return value
	try:
		from core.flags import is_secret_flag
		from core.secret_scrub import scrub_secret_shapes
		from core.secrets import is_secret_key
		secret = (is_secret_key(str(name)) or is_secret_flag(str(name))
		          or scrub_secret_shapes(text) != text)
	except Exception:  # noqa: BLE001 — fail closed: mask what cannot be checked
		secret = True
	if not secret:
		return value
	from core.security.redaction import fingerprint
	return fingerprint(text)


class PreferencesActionMixin:
	def _register_preferences_action(self):
		"""Register the agent-callable `preferences` tool (owner-UX P2 T2). Its
		reads register on every posture (095 W1c); `set` only
		under PREFS_TOOL_ENABLED (default OFF; ON under POLYROB_LOCAL).

		Lets the agent read/change the tenant's typed conversational preferences
		(``core.prefs.PREF_SCHEMA``) — the conversational front-end to what `/config` does in the CLI. Safety:

		- SAFE-sensitivity keys write immediately via ``write_preference`` (the
		  same writer `/config set` uses) and report like it does. GUARDED keys (budget/approval caps,
		  the owner's protective ceilings) can NEVER be written directly by the
		  agent — `set` instead quarantines a ``{user_id, key, value}`` proposal
		  (``core.prefs.propose_pref_change``) that rides the SAME owner
		  pending/approve pipeline as skills/self-context (Task 3's seam); the
		  active value never changes until the owner reviews it.
		- A forged/autonomous turn (self-wake or async-delegation-result
		  re-entry, a sub-agent/leaf worker, or an autonomous goal/cron run — the
		  SAME `_is_forged_or_autonomous_turn` check `skill_manage`/
		  `self_context_manage` use) has `set` refused OUTRIGHT, before anything
		  is validated or written — unlike skills/self-context, a SAFE
		  preference's `set` has no quarantine step of its own (it applies
		  immediately), so provenance-tagging alone would not be a safe
		  mitigation here. `list`/`get` are unaffected (a background turn can
		  still read its own effective config).
		- A prose rule is not a preference: it has ONE writer, `owner_doc_manage`
		  (095 W10 deleted the second one, `contract_propose`).
		- A leaf/sub-agent additionally never sees this tool at all — it is
		  excluded from the child's registry via
		  `tools.controller.delegation.delegation_exclusions_for_child`
		  (defence-in-depth on top of the runtime check above).
		- A correspondent-tainted session is denied the WHOLE action (including
		  `list`/`get`) by the pre-tool-call correspondent gate
		  (`agents/task/agent/core/correspondent_gate.py`), the same way
		  `skill_manage`/`self_context_manage` are.

		Registered in this module — no `from __future__ import annotations`
		(registry-closure introspection).
		"""
		# 095 W1c (SC-1, D2): reading is not a power — get/list/explain register
		# on EVERY posture; set stays behind PREFS_TOOL_ENABLED.
		# A room and a correspondent are still blocked (core/verb_policy_rows.py).
		try:
			from core.config_policy import prefs_tool_enabled
			writes = prefs_tool_enabled()
		except Exception:
			writes = False

		from typing import Literal as _Literal

		_KEY_DOC = ("Setting key (e.g. 'style.verbosity'); explain also takes an env flag "
		            "(e.g. 'TWITTER_ENABLED')")

		class PreferencesAction(BaseModel):
			operation: _Literal["list", "get", "set", "explain"]
			key: Optional[str] = Field(None, description=_KEY_DOC)
			value: Optional[str] = Field(
				None, description="set only: new value (coerced per the key's type)")

		class ReadPreferences(BaseModel):
			operation: _Literal["list", "get", "explain"]
			key: Optional[str] = Field(None, description=_KEY_DOC)

		_READS = ("operation='list' shows every preference (value/source/applies); 'get' "
		          "with `key` shows one; 'explain' with `key` shows the provenance chain of "
		          "ANY setting (preference or env flag) — use it to answer 'why is X off?'.")
		if writes:
			model, doc = PreferencesAction, (
				"Read and change your typed preferences (approvals, budget caps, goal "
				"quotas, digest/delivery, reply style, session defaults). " + _READS +
				" 'set' with `key`+`value`: a SAFE key applies from your next step unless "
				"the result says next session; a GUARDED key (budget/approval "
				"ceiling) only queues a proposal for the owner (/pending). ")
		else:
			model, doc = ReadPreferences, (
				"Read your typed preferences and settings (read-only here). " + _READS +
				" A change is the owner's: propose_setting.")

		@self.registry.action(doc, param_model=model)
		async def preferences(params: BaseModel, execution_context=None) -> ActionResult:
			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
			if not user_id:
				return ActionResult(error="preferences requires a user (tenant scope).",
				                    include_in_memory=True)

			data_dir = prefs_home_dir()  # the ONE identity home (prefs writers use it)
			from core.instance import resolve_instance_id
			instance_id = resolve_instance_id()

			from core.prefs import PREF_SCHEMA, SENSITIVITY_GUARDED, display_effective, unknown_pref_error

			if params.operation == "list":
				lines: list = []
				by_group: dict = {}
				for key in sorted(PREF_SCHEMA):
					by_group.setdefault(key.split(".", 1)[0], []).append(key)
				for group in sorted(by_group):
					lines.append(f"[{group}]")
					for key in by_group[group]:
						spec = PREF_SCHEMA[key]
						value, source = display_effective(key, user_id, data_dir, instance_id)
						tag = " [guarded]" if spec.sensitivity == SENSITIVITY_GUARDED else ""
						lines.append(f"  {key} = {_shown(key, value)}   ({source}, applies: "
						             f"{spec.applies}){tag}")
					lines.append("")
				return ActionResult(extracted_content="\n".join(lines).rstrip(),
				                    include_in_memory=True)

			if params.operation == "explain":
				# 018 P4: read-only provenance for ANY setting — pref OR catalog
				# env flag — via the config service (secrets masked there; the
				# scrubber below is defence-in-depth). Answers "why is X off?"
				# with the chain instead of a guess.
				if not params.key:
					return ActionResult(
						error="explain requires `key` (the setting name), e.g. "
						      "operation='explain', key='TWITTER_ENABLED'.",
						include_in_memory=True)
				try:
					from core import config_service
					info = config_service.explain(params.key, user_id=user_id,
					                              home_dir=data_dir)
				except KeyError:
					return ActionResult(error=config_service.unknown_key_message(params.key),
					                    include_in_memory=True)
				out = [f"{info.key} = {_shown(info.key, info.effective)}   ({info.source})"  # 093 A4 ↓
				       + (f" · {info.gate_effective}" if info.gate_effective else ""),
				       f"namespace: {info.namespace} | kind: {info.kind} | "
				       f"applies: {info.applies} | enforcement: {info.enforcement}"]
				if info.description:
					out.append(info.description)
				if info.chain:
					out.append("provenance (highest wins):")
					out.extend(f"  {s.origin}: {_shown(info.key, s.value)}" for s in info.chain)
				from core.secret_scrub import scrub_secret_shapes
				return ActionResult(extracted_content=scrub_secret_shapes("\n".join(out)),
				                    include_in_memory=True)

			if params.operation == "get":
				if not params.key:
					return ActionResult(error="get requires `key`.", include_in_memory=True)
				spec = PREF_SCHEMA.get(params.key)
				if spec is None:
					err = unknown_pref_error(params.key)
					return ActionResult(error=err, include_in_memory=True)
				value, source = display_effective(params.key, user_id, data_dir, instance_id)
				out = [f"{params.key} = {_shown(params.key, value)}   ({source})",
				       f"applies: {spec.applies}"]
				if spec.description:
					out.append(spec.description)
				if spec.sensitivity == SENSITIVITY_GUARDED:
					out.append("sensitivity: guarded — changing this queues a proposal "
					           "for owner review, it is never applied directly")
				return ActionResult(extracted_content="\n".join(out), include_in_memory=True)

			# set is the one write operation. A SAFE key writes the ACTIVE
			# preferences.toml immediately — there is no pending/promote step, so a
			# forged turn is refused OUTRIGHT. A GUARDED key never writes active state.
			is_forged = _is_forged_or_autonomous_turn(execution_context, self)

			_self_mod_ev = self_mod_emitter(
				execution_context, self, user_id,
				kind="pref", source="preferences", created_by="agent")

			if params.operation == "set":
				if is_forged:
					return ActionResult(
						error=("preferences: set is not permitted for forged/autonomous "
						      "turns (the owner must be in the loop)."),
						include_in_memory=True)
				if not params.key or params.value is None:
					return ActionResult(error="set requires `key` and `value`.",
					                    include_in_memory=True)
				spec = PREF_SCHEMA.get(params.key)
				if spec is None:
					err = unknown_pref_error(params.key, params.value)
					return ActionResult(error=err, include_in_memory=True)

				if spec.sensitivity == SENSITIVITY_GUARDED:
					from core.prefs import propose_pref_change
					ok, result = propose_pref_change(user_id, params.key, params.value,
					                                 data_dir, instance_id=instance_id)
					if not ok:
						return ActionResult(error=result, include_in_memory=True)
					_self_mod_ev("propose", result, pending=True)
					return ActionResult(
						extracted_content=(
							f"'{params.key}' is guarded — queued for owner review "
							f"(proposal {result}) — review with /pending."),
						include_in_memory=True)

				from core.prefs import write_preference
				ok, err = write_preference(data_dir, user_id, params.key, params.value,
				                           instance_id=instance_id)
				if not ok:
					return ActionResult(error=err, include_in_memory=True)
				_self_mod_ev("set", params.key, pending=False)
				# 095 W1c (SC-3): the same report `/config set` prints — the value in
				# force, when it applies, and a write a higher layer makes a no-op.
				from core.config_service import pref_written_report
				message, _notes = pref_written_report(params.key, params.value, user_id, data_dir)
				return ActionResult(extracted_content=message, include_in_memory=True)
