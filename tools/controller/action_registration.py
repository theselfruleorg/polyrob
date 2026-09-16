"""ActionRegistrationMixin — Controller one-shot action registration (UP-11, verbatim
code-motion). Registers send_message/done/load_skill/session_search/memory/delegation
closures + backward-compat aliases. Methods use ``self`` (the composed Controller)
exactly as before via the MRO."""
import logging
import os
from typing import Any, Dict, List, Optional

from core.env import bool_env as _bool_env
from core.runtime_paths import data_dir_or_home

from pydantic import BaseModel, Field

from tools.controller.types import ActionResult
from tools.controller.views import DoneAction, SendMessageAction
from tools.controller.doc_authoring import DocAuthoringMixin
from tools.controller._helpers import (
	build_load_skill_result,
	build_session_search_hint,
	read_skill_resource_confined,
	self_mod_emitter,
)
from modules.llm.messages import AIMessage
from tools.controller.turn_origin import (  # noqa: F401 — re-exported: external
	# callers (crypto_trade_gate, defi/trade_tool, approval_queue) and tests do a
	# lazy `from tools.controller.action_registration import ...` at call time,
	# which reads THIS module's namespace — so monkeypatching here still works.
	_MESSAGE_TEXT_PREVIEW_CHARS,
	_autonomous_message_refusal,
	_autonomous_owner_resend_cooldown_refusal,
	_is_autonomous_goal_turn,
	_is_forged_or_autonomous_turn,
	_message_action_result,
)

logger = logging.getLogger(__name__)

# Emit-side glue (route-outcome honesty + the mirror's session context) lives in
# tools/controller/emit.py — this module is a god-file under a shrink-only size
# ratchet, so new behaviour goes in a new module. Imported under the original
# private names because existing call sites and a test use them.
from tools.controller.emit import (  # noqa: E402
	attachment_receipt,
	_describe_route_outcome,
	_publish_context,
	_ROUTE_OUTCOME_NOT_DELIVERED,  # noqa: F401  (re-export for existing importers)
)


class ActionRegistrationMixin(DocAuthoringMixin):
	def _register_default_actions(self):
		"""Register core 'done' action for task completion.

		The 'done' action is a CORE completion primitive that must always be available,
		regardless of which tools are loaded. It opportunistically checks TodoManager
		if TaskTool is available, but never blocks completion.

		Todo-specific actions (task_todo_list, task_todo_add, etc.) remain in TaskTool.
		"""
		# Register the core 'done' action
		# Support multiple output models for flexibility
		if self.output_model is None:
			@self.registry.action(
				'Send a message to the user (status update, question, or information)',
				param_model=SendMessageAction
			)
			async def send_message(params: SendMessageAction, execution_context=None):
				"""Send message to user - continuous chat mode.

				When wait_for_response=True:
				- Message is sent to user
				- Execution STOPS (returns is_done=True)
				- User replies via continuous chat whenever they want
				- Session resumes with reply in context

				No timeouts, no waiting loops, no retries.
				
				SUB-AGENT ISOLATION: Sub-agents skip adding to main context and feed.
				Their messages are captured in their own context and returned as output.
				"""
				# C5: what became of any workspace file this message named. Initialised
				# here so every return path is safe on branches that skip the mirror.
				_attach_note = None
				import time
				
				# Check if this is a sub-agent (skip side effects for isolation)
				is_sub_agent = execution_context.is_sub_agent if execution_context else False
				
				if is_sub_agent:
					# Sub-agent: Just log and return - output captured via normal result flow
					self.logger.debug(f"💬 Sub-agent message (captured in result): {params.text[:100]}...")
					return ActionResult(
						extracted_content=f"Message: {params.text}",
						include_in_memory=True
					)

				self.logger.info(f"💬 Message to user: {params.text[:100]}...")

				# Add to conversation history - find the correct agent by agent_id
				if hasattr(self, 'orchestrator') and self.orchestrator:
					target_agent = None
					agent_id = execution_context.agent_id if execution_context else None
					
					if agent_id and agent_id in self.orchestrator.agents:
						target_agent = self.orchestrator.agents[agent_id]
					else:
						# Fallback: use first agent (legacy behavior)
						agents = list(self.orchestrator.agents.values())
						if agents:
							target_agent = agents[0]
					
					if target_agent and hasattr(target_agent, 'message_manager'):
						try:
							target_agent.message_manager._add_message_with_tokens(
								AIMessage(content=params.text),
								_internal=True  # Bypass single-writer enforcement for display messages
							)
							self.logger.debug("Added message to conversation history")
						except Exception as e:
							self.logger.warning(f"Could not add message to history: {e}")

				# Add to session feed for WebView
				if hasattr(self, 'orchestrator') and self.orchestrator:
					try:
						self.orchestrator.session_manager.add_to_feed(
							self.session_id,
							'agent_message',
							{
								'text': params.text,
								'wait_for_response': params.wait_for_response,
								'timestamp': time.time()
							}
						)
					except Exception as e:
						self.logger.debug(f"Could not add to feed: {e}")

					# P1a outbound-collapse: mirror the discrete message into the unified
					# MessageRouter seam (committed/partial=False) when a router + chat
					# session_key are bound (later phases). No-op + fail-open otherwise.
					try:
						from core.surfaces.outbound_mirror import build_discrete_publish
						from core.surfaces.turn_reply import mark_reply_published
						_mirror = build_discrete_publish(
							getattr(self.orchestrator, "_message_router", None),
							getattr(self.orchestrator, "_chat_session_key", None),
							**_publish_context(self, reply_to=params.reply_to),
						)
						_resolution = await _mirror(params.text)
						# C1: send_message is the speech verb — it claims this turn's
						# reply (done's mirror then stands down) and records the text,
						# which the unbound delivery path needs. See core.surfaces.turn_reply.
						mark_reply_published(self.orchestrator, params.text)
						# C5: tell the AGENT what became of the files it named.
						_attach_note = attachment_receipt(_resolution)
					except Exception as e:
						self.logger.debug(f"Could not mirror message to router: {e}")

					# §3.1 (intelligence-stack finalization): in an AUTONOMOUS session
					# (goal/cron/planner) there is no interactive surface — the message
					# used to die in the session feed (the live case's lost blocker
					# report). Route it to the session's OWN principal through the one
					# delivery rail (dedup + rate caps + durable fallback). Strict
					# scope: own principal only; arbitrary recipients stay the gated
					# `message` tool's job. Fail-open — never fails the action.
					_route_outcome = None
					try:
						from core.surfaces.user_delivery import maybe_deliver_autonomous_send
						_route_outcome = await maybe_deliver_autonomous_send(
							self.orchestrator, self.session_id, params.text)
						if _route_outcome is not None:
							self.logger.info(
								f"💬 Autonomous send_message routed to user: {_route_outcome}")
					except Exception as e:
						self.logger.debug(f"Autonomous send routing failed (fail-open): {e}")

				# CONTINUOUS CHAT MODE: If waiting for response, stop execution
				# User will reply via continuous chat, session will resume with their response
				if params.wait_for_response:
					# P3 (Task 3.2): guard the wait_for_response deadlock. If a chat surface
					# is bound AND it cannot collect a reply (supports_interactive_ask=False),
					# pausing the session would hang it forever. Degrade to a non-blocking
					# notify in that case. Unknown/unbound (the legacy default) -> pause as
					# before, so flag-OFF behavior is byte-identical. Lazy import: this file
					# must never get `from __future__ import annotations` (registry closures).
					try:
						from core.surfaces.binding import surface_ask_capability
						_ask_ok = surface_ask_capability(getattr(self, 'orchestrator', None))
					except Exception:
						_ask_ok = None
					if _ask_ok is False:
						self.logger.info("💬 Message sent. Surface can't collect a reply; not pausing (non-blocking notify).")
						return ActionResult(
							extracted_content="Message sent to user. This surface cannot collect a reply, so the task was not paused.",
							include_in_memory=True,
							metadata={'conversational_reply': True}
						)
					self.logger.info("💬 Message sent. Stopping execution to wait for user response via continuous chat")
					_route_status = _describe_route_outcome(_route_outcome)
					_paused_summary = (f"Message sent to user. Task paused - will resume when user responds. — {_route_status}"
						if _route_status else "Message sent to user. Task paused - will resume when user responds.")
					if _attach_note:
						_paused_summary = f"{_paused_summary} {_attach_note}"
					return ActionResult(
						extracted_content=_paused_summary,
						include_in_memory=True,
						is_done=True,  # Stop execution, wait for user via continuous chat
						metadata={'delivery_outcome': _route_outcome}
					)

				# Non-blocking message sent.
				# R1: tag it so the run loop can recognize a turn whose only output was a
				# user-facing reply (no productive tool work) and end it — otherwise a
				# chat reply loops and re-greets, since non-blocking sends keep is_done False.
				_route_status = _describe_route_outcome(_route_outcome)
				_send_summary = (f"Message sent to user (non-blocking) — {_route_status}"
					if _route_status else "Message sent to user (non-blocking)")
				if _attach_note:
					_send_summary = f"{_send_summary} {_attach_note}"
				return ActionResult(
					extracted_content=_send_summary,
					include_in_memory=False,
					metadata={'conversational_reply': True, 'delivery_outcome': _route_outcome}
				)

			@self.registry.action('Complete the current task and provide final output', param_model=DoneAction)
			async def done(params: DoneAction, execution_context=None):
				"""Complete the current task.
				
				SUB-AGENT ISOLATION: Sub-agents skip adding to main context and feed.
				Their completion is captured in the result and returned to parent.
				"""
				import time
				
				# Check if this is a sub-agent (skip side effects for isolation)
				is_sub_agent = execution_context.is_sub_agent if execution_context else False
				completion_msg = params.text or "Task completed"
				
				if is_sub_agent:
					# Sub-agent: Just return done - output captured via normal result flow
					self.logger.debug(f"✅ Sub-agent done: {completion_msg[:100]}...")
					return ActionResult(is_done=True, extracted_content=completion_msg)
				
				# Main agent: Full processing with todos check and feed updates
				# Opportunistically check todos if TaskTool available
				task_tool = self.get_tool('task')
				if task_tool and hasattr(task_tool, '_get_or_create_todo_manager'):
					try:
						todo_mgr = task_tool._get_or_create_todo_manager(
							self.session_id,
							getattr(self, 'user_id', None)
						)
						if todo_mgr:
							progress = todo_mgr.get_progress()
							if progress['total'] > 0 and progress['percentage'] < 80:
								warning_msg = (
									f"⚠️ WARNING: Completing with only {progress['percentage']:.0f}% "
									f"todos done ({progress['completed']}/{progress['total']}). "
									f"Consider completing more todos for thorough task completion."
								)
								self.logger.warning(warning_msg)
								return ActionResult(
									is_done=True,
									extracted_content=f"{params.text}\n\n{warning_msg}"
								)
					except Exception as e:
						self.logger.debug(f"Could not check todo progress: {e}")
						# Continue anyway - don't block completion

				# Add completion message to conversation history - find correct agent
				if hasattr(self, 'orchestrator') and self.orchestrator:
					target_agent = None
					agent_id = execution_context.agent_id if execution_context else None
					
					if agent_id and agent_id in self.orchestrator.agents:
						target_agent = self.orchestrator.agents[agent_id]
					else:
						# Fallback: use first agent (legacy behavior)
						agents = list(self.orchestrator.agents.values())
						if agents:
							target_agent = agents[0]
					
					if target_agent and hasattr(target_agent, 'message_manager'):
						try:
							target_agent.message_manager._add_message_with_tokens(
								AIMessage(content=f"✅ Task Complete\n\n{completion_msg}"),
								_internal=True  # Bypass single-writer enforcement for display messages
							)
							self.logger.info("Added completion message to conversation history")
						except Exception as e:
							self.logger.warning(f"Could not add completion to history: {e}")
					
					# Add to session feed for WebView
					try:
						self.orchestrator.session_manager.add_to_feed(
							self.session_id,
							'task_complete',
							{
								'message': completion_msg,
								'timestamp': time.time()
							}
						)
					except Exception as e:
						self.logger.debug(f"Could not add completion to feed: {e}")

					# C10: mirror the completion text into the unified MessageRouter seam so a
					# bound chat surface (Singular Chat) delivers it once — matching send_message.
					# Without this, the harness skipping its post-run deliver for bound sessions
					# would silence done()-terminated turns (done doesn't otherwise reach the
					# router). No-op + fail-open when unbound (build_discrete_publish → no router).
					#
					# C1: this mirror is now the turn's SAFETY NET, not a second voice —
					# it publishes only when the turn spoke nothing, so a done()-only turn
					# still reaches the user while an answer-then-recap turn delivers one
					# message instead of two. Gate CHAT_SINGLE_FINAL.
					try:
						from core.surfaces.outbound_mirror import build_completion_publish
						_mirror = build_completion_publish(
							getattr(self.orchestrator, "_message_router", None),
							getattr(self.orchestrator, "_chat_session_key", None),
							self.orchestrator,
							**_publish_context(self),
						)
						await _mirror(completion_msg)
					except Exception as e:
						self.logger.debug(f"Could not mirror completion to router: {e}")

				self.logger.info(f'Task marked as done: {completion_msg}')
				return ActionResult(is_done=True, extracted_content=completion_msg)

		elif self.output_model is not None:
			# For agents with custom output models
			@self.registry.action('Complete the current task with structured output', param_model=self.output_model)
			async def done(_params: BaseModel):
				self.logger.info(f'Task marked as done with structured output')
				return ActionResult(is_done=True)

		# S-1: progressive skill disclosure. When enabled, the system injects only a
		# compact <skill-catalog>; the agent pulls a skill's full body on demand with
		# this tool. Off by default (skills are eager-injected full-body, no tool).
		from agents.task.constants import skill_progressive_disclosure
		if skill_progressive_disclosure():
			class LoadSkillAction(BaseModel):
				skill_id: str

			@self.registry.action(
				"Load a skill's full instructions on demand (see <skill-catalog>). "
				"Call before doing work the skill covers.",
				param_model=LoadSkillAction,
			)
			async def load_skill(params: LoadSkillAction, execution_context=None):
				# Task 17: resolve the skill's on-disk dir (fail-open to None — an
				# in-memory-only skill or a lookup error just means no resource list
				# is advertised, not a broken load_skill).
				skill_dir = None
				uid = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
				_skill_manager = None
				try:
					from agents.task.agent.skill_manager import get_skill_manager
					_skill_manager = get_skill_manager()
					skill_dir = _skill_manager.resolve_skill_dir(params.skill_id, uid)
				except Exception:
					skill_dir = None
				result = build_load_skill_result(
					getattr(self, '_session_skills', {}), params.skill_id,
					activated=getattr(self, '_activated_skills', None),
					skill_dir=skill_dir,
				)
				# SK-F1: a skill that never made it into (or was capped out of)
				# _session_skills is still on disk — fall back to a tenant-scoped
				# disk load via the manager's existing resolver (excludes
				# `.pending/` drafts, since those live at a different path)
				# instead of erroring on every skill the eager/catalog pass didn't
				# preload. Only attempted on the "unknown id" failure shape.
				if result.error and _skill_manager is not None:
					try:
						sid = (params.skill_id or "").strip().strip('"')
						# SK-F1: sid is LLM-controlled and _load_skill_content does no
						# traversal filtering of its own (it just Path-joins). Gate the
						# fallback on resolve_skill_dir's existing guard (rejects '/',
						# '\\', '..', whitespace-padding, absolute-path-shaped ids) so a
						# crafted id like "../other_uid/secret-skill" cannot escape into
						# another tenant's directory. A rejected id falls through with
						# content="" -> same "Unknown skill_id" error shape as before,
						# no exception leaks to the model.
						content = ""
						# P1-1: make the auto_activate gate REAL on the load path. A gated
						# skill (auto_activate:false — the money/trading playbooks) is only
						# loadable when the session has actually loaded its required tools;
						# otherwise refuse (same "Unknown skill_id" shape) so a model that
						# guesses the id cannot pull the gated playbook. A legitimately-
						# available gated skill is preloaded into _session_skills via the
						# catalog and never reaches this fallback.
						_loaded_tool_ids = []
						try:
							_loaded_tool_ids = self.list_tools()
						except Exception:
							_loaded_tool_ids = []
						_gate_ok = True
						try:
							_gate_ok = _skill_manager.may_load_skill(sid, tool_ids=_loaded_tool_ids, user_id=uid)
						except Exception:
							_gate_ok = True  # fail-open on the gate check; path/tenant guards still apply
						if _gate_ok and _skill_manager.resolve_skill_dir(sid, uid) is not None:
							content = _skill_manager._load_skill_content(sid, user_id=uid)
						if content:
							from agents.task.agent.skill_manager import MatchedSkill
							fallback_skill = MatchedSkill(
								skill_id=sid,
								priority=0,
								match_reasons=["disk-fallback"],
								content=content,
							)
							# P2-22: register the fallback-loaded skill into _session_skills
							# so a follow-up read_skill_resource (which gates on membership
							# there) can serve the resources this load result advertises —
							# previously it refused "Call load_skill first" for a skill the
							# model had JUST loaded via the disk fallback.
							_ss = getattr(self, '_session_skills', None)
							if isinstance(_ss, dict):
								_ss[sid] = fallback_skill
							result = build_load_skill_result(
								{sid: fallback_skill}, sid,
								activated=getattr(self, '_activated_skills', None),
								skill_dir=skill_dir,
							)
					except Exception:
						pass
				if result.metadata and result.metadata.get('skill_loaded'):
					self.logger.info(f"📖 Loaded skill on demand: {result.metadata['skill_loaded']}")
					# W2-D: provenance/reuse metric — bumped at the closure (which has
					# execution_context), NOT the pure build_load_skill_result helper.
					# Fail-open: a metrics write must never break a skill load.
					try:
						uid = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
						if uid:
							from modules.skills.skill_usage import get_skill_usage_store
							# SK-F1: use the actually-resolved/loaded id
							# (result.metadata['skill_loaded'] is always the
							# stripped/unquoted sid -- set identically by
							# build_load_skill_result on both the eager and
							# disk-fallback paths), not the raw params.skill_id,
							# so quote/whitespace-padded ids don't fragment
							# curator metrics under two different keys. The enclosing
							# `if ... get('skill_loaded')` guarantees this is truthy
							# (D1: the old `or params.skill_id` fallback was dead).
							loaded_id = result.metadata['skill_loaded']
							get_skill_usage_store().bump_load(loaded_id, uid)
					except Exception:
						pass
				return result

			# Task 17: read-only resource access for an already-loaded skill (references/
			# assets, e.g. an authored skill's REFERENCE.md). NEVER executes anything —
			# a skill's scripts/*.sh are readable as text, never run. Gated by the same
			# SKILL_PROGRESSIVE_DISCLOSURE flag as load_skill since it's only useful once
			# _session_skills is populated (refusal is a no-op otherwise).
			class ReadSkillResourceAction(BaseModel):
				skill_id: str
				resource_path: str

			@self.registry.action(
				"Read a resource file (reference doc, asset, etc.) that belongs to a skill "
				"you have already loaded via load_skill (see metadata.skill_resources on that "
				"call's result for the available paths). Read-only — this NEVER executes "
				"scripts or other files, it only returns their text content.",
				param_model=ReadSkillResourceAction,
			)
			async def read_skill_resource(params: ReadSkillResourceAction, execution_context=None):
				session_skills = getattr(self, '_session_skills', {}) or {}
				sid = (params.skill_id or "").strip().strip('"')
				if sid not in session_skills:
					available = ", ".join(session_skills.keys()) or "(none)"
					return ActionResult(
						error=(
							f"Skill '{sid}' is not loaded this session (available: {available}). "
							"Call load_skill first."
						),
						include_in_memory=True,
					)
				skill_dir = None
				try:
					from agents.task.agent.skill_manager import get_skill_manager
					uid = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
					skill_dir = get_skill_manager().resolve_skill_dir(sid, uid)
				except Exception:
					skill_dir = None
				if skill_dir is None:
					return ActionResult(
						error=f"Skill '{sid}' has no on-disk resources available.",
						include_in_memory=True,
					)
				ok, content = read_skill_resource_confined(skill_dir, params.resource_path)
				if not ok:
					return ActionResult(error=content, include_in_memory=True)
				return ActionResult(extracted_content=content, include_in_memory=True)

		# S2 (dynamic tool rig, 2026-07-19): self-serve tool loading. The honest
		# <tool-catalog> foundation block (S1) advertises every known tool with a
		# status; this action materializes a `loadable` one mid-session through the
		# SAME load_tools_from_container path session creation uses. Decision logic
		# (money never loadable, leaf blocklist, structured refusals with remedy)
		# lives in tools/tool_disclosure.py::perform_load_tool — the closure stays
		# thin, mirroring the `message` action.
		from core.config_policy import tool_progressive_disclosure
		if tool_progressive_disclosure():
			# load_tool (S2) + tool_search/tool_describe (Tier-3 item 1) share one
			# thin registration seam, extracted to tool_search_actions.py so this
			# god-file (size ratchet) does not grow. Logic: tool_disclosure.py /
			# tool_search.py.
			from tools.controller.tool_search_actions import register_dynamic_tool_actions
			register_dynamic_tool_actions(self)

		# P0-1: agent-callable cross-session recall. Registered only when an external
		# memory provider is active (MEMORY_BACKEND=sqlite); inert in the default config
		# (NullMemoryProvider), so the tool never appears unless a backend is configured.
		self._register_session_search_action()

		# UP-09: optional bounded curated memory tool (read/add/remove), gated
		# MEMORY_TOOL_ENABLED (default false) AND external provider active.
		self._register_memory_tool_action()

		# Task 4: agent-callable recall of the agent's OWN activity (chat/goal/cron
		# episodes), gated EPISODIC_MEMORY_ENABLED AND external provider active.
		self._register_recent_activity_action()

		# W2: optional writable-skills tool (create/patch/delete), gated SKILLS_WRITABLE
		# (default false). Lets the agent author durable procedures; quarantined to
		# .pending/ + threat-scanned + tenant-confined.
		self._register_skill_manage_action()

		# polyrob C-write: optional evolving SELF-identity tool, gated
		# SELF_CONTEXT_WRITABLE (default false; ON under POLYROB_LOCAL). Lets the agent
		# refine its per-(instance,user) self.md; identity-scanned + .pending +
		# tenant-confined; SOUL tier stays operator-only.
		self._register_self_context_manage_action()

		# Bounded owner-facts doc (USER.md-equivalent), gated OWNER_DOC_WRITABLE
		# (default false; ON under POLYROB_LOCAL). Same quarantine-then-promote model.
		self._register_owner_doc_manage_action()

		# owner-UX P2 T2: agent-callable `preferences` action (typed per-user
		# config + operating-contract proposals), gated PREFS_TOOL_ENABLED
		# (default false; ON under POLYROB_LOCAL).
		self._register_preferences_action()

		# W7: optional read-only insights tool (authored-skill reuse %), gated
		# INSIGHTS_TOOL (default false). Tenant-scoped.
		self._register_insights_action()

		# I-6: read-only runtime self-introspection (steps/tools/context/wallet+
		# ledger), gated AGENT_STATUS_TOOL (default false; ON under POLYROB_LOCAL).
		self._register_agent_status_action()

		# Task 13 (Phase 3 R3): read-only tenant-scoped usage rollup + non-binding
		# invoice-draft suggestion, gated USAGE_INVOICE_BRIDGE_ENABLED (default
		# false; deliberately NOT in the POLYROB_LOCAL safe group — explicit
		# billing feature). Never creates a payment request itself.
		self._register_usage_summary_action()

		# T3-01: agent-initiated MCP install from the reviewed catalog, gated
		# MCP_SELF_INSTALL_ENABLED (default false). Owner-in-the-loop: forged/leaf
		# turns refused; approver is Deny-by-default unless APPROVAL_PROVIDER is set.
		self._register_mcp_install_action()

		# Task 5: gated `message` action (owner/allowlist -> MessageRouter send),
		# gated MESSAGE_TOOL_ENABLED (default false; ON under POLYROB_LOCAL).
		self._register_message_action()

		# E3 (2026-07-13 review): read-only durable per-correspondent history,
		# gated CORRESPONDENT_ACCESS_ENABLED.
		self._register_contact_history_action()

		# Register subtask action for sub-agent delegation
		self._register_subtask_action()

	def _register_session_search_action(self):
		"""Register `session_search` when a cross-session memory backend is active.

		Gives the agent on-demand recall over its own past sessions (tenant-scoped via
		user_id, P0-0). Mirrors Reference's agent-callable session_search. Fail-open and
		only advertised when an external provider is registered, so the default
		NullMemoryProvider config is byte-identical (no new tool surfaced).
		"""
		try:
			from modules.memory.registry import get_memory_registry
			provider = get_memory_registry().active()
			if provider is None or not getattr(provider, 'is_external', False):
				return  # no external backend -> don't surface the tool
		except Exception:
			return

		class SessionSearchAction(BaseModel):
			query: str = ""                      # empty => browse most-recent
			limit: int = 5                       # provider clamps to [1, 20]
			sort: Optional[str] = None           # "newest" | "oldest" | None (rank)
			collection: Optional[str] = None     # set to search a named knowledge-base collection instead of past sessions
			before_id: Optional[int] = Field(default=None, ge=1)  # T2.6: rowid cursor — pass the smallest id from a previous result to page further back
		_SEARCH_DESC = (
			"Recall your durable memory of PAST sessions. WHEN TO USE: reach for this "
			"BEFORE web/filesystem on 'what did we do about X', 'where did we leave Y', "
			"or 'what was I working on'. Two shapes: pass a `query` to DISCOVER relevant "
			"past work (facts, decisions); leave `query` empty to BROWSE your most-recent "
			"sessions. Optional `limit` (1-20) and `sort` ('newest'/'oldest'); each result line is tagged `(id N)` — with sort='newest', pass `before_id=<smallest id>` to page further back (in relevance-ranked mode, refine the query or switch to sort='newest' instead). "
			"Optional `collection`: set to search a named knowledge-base collection instead "
			"of past sessions (requires KB to be enabled)."
		)

		def _kb_on() -> bool:
			try:
				from core.config_policy import AutonomyConfig
				return AutonomyConfig.kb_enabled()
			except Exception:
				return False

		async def _run_session_search(params: SessionSearchAction, execution_context=None) -> ActionResult:
			# Tenant scoping + empty-user_id refusal are enforced in the provider
			# (_anon_blocked, UP-03); the controller does NOT re-read MEMORY_REQUIRE_USER_ID.
			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
			session_id = getattr(execution_context, 'session_id', None) or getattr(self, 'session_id', '')

			# Task 7: KB recall path — routes to kb_search when collection is set and KB is enabled.
			# Fail-open: any exception falls through to the normal memory_search path below.
			if params.collection and _kb_on():
				try:
					from modules.memory.registry import kb_search
					recalled = await kb_search(
						params.query, user_id=user_id,
						collection=params.collection, limit=params.limit,
					)
					if not recalled or not recalled.strip():
						return ActionResult(
							extracted_content=f"No knowledge-base results for {params.query!r} in {params.collection!r}",
							include_in_memory=True,
						)
					try:
						from core.security.untrusted_wrap import wrap_untrusted
						recalled = wrap_untrusted("knowledge_base", recalled)
					except Exception:
						pass  # fail-open
					return ActionResult(
						extracted_content=f"## Recalled from knowledge base\n{recalled}",
						include_in_memory=True,
					)
				except Exception as e:
					self.logger.debug(f"kb_search failed, falling back to memory_search: {e}")
					# fall through to normal session memory path

			try:
				from modules.memory.registry import memory_search
				recalled = await memory_search(
					params.query, session_id=session_id, user_id=user_id,
					limit=params.limit, sort=params.sort, before_id=params.before_id, with_ids=True,
				)
			except Exception as e:
				self.logger.debug(f"session_search failed: {e}")
				return ActionResult(extracted_content="No memory available.", include_in_memory=False)
			if not recalled or not recalled.strip():
				label = params.query if params.query else "(recent sessions)"
				return ActionResult(
					extracted_content=f"No past-session memory matched: {label!r}",
					include_in_memory=True,
				)
			# W6/UP-06: recalled cross-session content may carry indirect injection
			# (it can include previously-ingested web/tool output) — frame it as DATA.
			# session_search is NOT in the automatic untrusted-tool set, so wrap here.
			try:
				from core.security.untrusted_wrap import wrap_untrusted
				recalled = wrap_untrusted("session_search", recalled)
			except Exception:
				pass  # fail-open: never block recall on a wrap import error
			header = ("## Recalled from past sessions" if params.query
			          else "## Your most-recent sessions")
			return ActionResult(  # T2.6: hint from the `(id N)` tags recalled already carries
				extracted_content=f"{header}\n{recalled}{build_session_search_hint(recalled, params.limit, params.sort)}",
				include_in_memory=True,
			)

		@self.registry.action(_SEARCH_DESC, param_model=SessionSearchAction)
		async def session_search(params: SessionSearchAction, execution_context=None) -> ActionResult:
			return await _run_session_search(params, execution_context)

		# W6: Reference exposes the same recall under both names. Register `memory_search`
		# as a thin alias (default-on via MEMORY_SEARCH_TOOL) so prompts/tools that use
		# either shape resolve. Same handler, same tenant scoping — zero new surface.
		try:
			from core.config_policy import AutonomyConfig
			alias_on = AutonomyConfig.memory_search_tool()
		except Exception:
			alias_on = True
		if alias_on:
			@self.registry.action(_SEARCH_DESC, param_model=SessionSearchAction)
			async def memory_search(params: SessionSearchAction, execution_context=None) -> ActionResult:
				return await _run_session_search(params, execution_context)

	def _register_recent_activity_action(self):
		"""Register `recent_activity`: time-ordered recall of the agent's OWN runs
		(chat/goal/cron), tenant-scoped. Answers 'what did I do since X'. Only surfaced
		when EPISODIC_MEMORY_ENABLED and an external provider is active (byte-identical
		default config otherwise). NO `from __future__` in this module (registry-closure
		landmine)."""
		try:
			from core.config_policy import AutonomyConfig
			if not AutonomyConfig.episodic_memory_enabled():
				return
			from modules.memory.registry import get_memory_registry
			provider = get_memory_registry().active()
			if provider is None or not getattr(provider, 'is_external', False):
				return
		except Exception:
			return

		class RecentActivityAction(BaseModel):
			since: Optional[str] = None      # "8h" | "2d" | "30m" | ISO8601; None => last 24h
			until: Optional[str] = None
			kind: Optional[str] = None       # "chat" | "goal" | "cron"
			limit: int = 20

		_DESC = (
			"Recall YOUR OWN recent activity — the goals, cron jobs, and chats you ran, "
			"newest-first, with outcome and spend. USE THIS (not the filesystem, not notes) "
			"for 'what did I do', 'what ran since X', 'what have I been up to'. Optional "
			"`since` ('8h'/'2d'/ISO), `kind` ('goal'/'cron'/'chat'), `limit` (1-20)."
		)

		async def _run_recent_activity(params, execution_context=None) -> ActionResult:
			from modules.memory.registry import memory_recall_episodes
			from modules.memory.episodic import parse_since
			import time as _t
			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
			since_ts = (parse_since(params.since) if params.since else None) or (int(_t.time()) - 24 * 3600)
			until_ts = parse_since(params.until) if params.until else None
			rows = await memory_recall_episodes(
				user_id=user_id, since_ts=since_ts, until_ts=until_ts,
				kind=params.kind, limit=params.limit, order="newest")
			if not rows:
				return ActionResult(
					extracted_content="No recorded activity in that window.",
					include_in_memory=True)
			lines, total = [], 0.0
			for e in rows:
				total += float(e.spend_usd or 0)
				arts = ", ".join(a.get("path", "") for a in (e.artifacts or [])[:3])
				lines.append(
					f"- {e.kind}:{e.outcome or '?'} ${float(e.spend_usd or 0):.2f} "
					f"\"{(e.task or '')[:60]}\"" + (f" -> {arts}" if arts else ""))
			body = "\n".join(lines) + f"\n({len(rows)} runs, ${total:.2f} total)"
			try:
				from core.security.untrusted_wrap import wrap_untrusted
				body = wrap_untrusted("recent_activity", body)
			except Exception:
				pass
			return ActionResult(extracted_content=f"## Your recent activity\n{body}",
			                    include_in_memory=True)

		@self.registry.action(_DESC, param_model=RecentActivityAction)
		async def recent_activity(params: RecentActivityAction, execution_context=None) -> ActionResult:
			return await _run_recent_activity(params, execution_context)

	def _register_memory_tool_action(self):
		"""Register the bounded curated `memory` tool (UP-09) when enabled.

		Gated on MEMORY_TOOL_ENABLED (default false) AND an external memory provider
		with a curated store. Tenant scoping + empty-user_id refusal live in the
		provider (UP-03 _anon_blocked). v1: read/add/remove only, no system-prompt
		snapshot (keeps the cached prompt untouched, PR13 principle).
		"""
		if not _bool_env("MEMORY_TOOL_ENABLED", False):
			return
		try:
			from modules.memory.registry import get_memory_registry
			provider = get_memory_registry().active()
			if provider is None or not getattr(provider, 'is_external', False):
				return
			if not hasattr(provider, 'curated_add'):
				return  # provider has no curated store
		except Exception:
			return

		from typing import Literal as _Literal

		class MemoryToolAction(BaseModel):
			action: _Literal["read", "add", "remove", "create", "update", "archive",
			                 "list", "show"]
			content: Optional[str] = None   # add/create/update body; remove substring
			note_id: Optional[int] = None   # update/archive/show target
			title: Optional[str] = None     # create/update
			tags: Optional[str] = None      # create/update, comma-separated

		@self.registry.action(
			"Your durable, curated long-term notes (separate from automatic session "
			"recall). action='create' to save a note with title/tags — link related "
			"notes with [[wikilinks]] in the body; action='update'/'archive' by "
			"note_id; action='list' to browse; action='show' for one note with "
			"backlinks; legacy 'add'/'read'/'remove' still work. Keep notes short "
			"and factual.",
			param_model=MemoryToolAction,
		)
		async def memory(params: MemoryToolAction, execution_context=None) -> ActionResult:
			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
			from modules.memory.registry import get_memory_registry
			prov = get_memory_registry().active()
			if prov is None or not hasattr(prov, 'curated_add'):
				return ActionResult(extracted_content="Memory tool unavailable.", include_in_memory=False)
			from core.security.untrusted_wrap import wrap_untrusted

			def _fmt_ts(ts):
				try:
					import time as _t
					return _t.strftime("%Y-%m-%d", _t.localtime(int(ts))) if ts else "?"
				except Exception:
					return "?"

			if params.action == "read":
				notes = await prov.curated_read(user_id)
				if notes.strip():
					# P1-7: curated notes are agent-authored (possibly under injection
					# influence) and read back in FUTURE sessions — a durable persistence-
					# laundering channel. Frame as untrusted DATA like every other recall
					# surface (session_search/kb_search/recent_activity).
					content = f"## Your curated memory\n{wrap_untrusted('memory', notes)}"
				else:
					content = "Your curated memory is empty."
				return ActionResult(extracted_content=content, include_in_memory=True)

			if params.action == "list":
				notes = await prov.note_list(user_id)
				pending = await prov.note_list(user_id, status="pending")
				if not notes and not pending:
					return ActionResult(extracted_content="No notes yet.", include_in_memory=True)
				lines = []
				for n in notes:
					tag_s = f" [{', '.join(n['tags'])}]" if n["tags"] else ""
					title_s = n["title"] or "(untitled)"
					lines.append(f"#{n['id']} {title_s}{tag_s} ({_fmt_ts(n['updated_ts'])}): "
					             f"{(n['content'] or '')[:120]}")
				body = "\n".join(lines)
				if pending:
					body += f"\n({len(pending)} pending note(s) awaiting owner review)"
				return ActionResult(
					extracted_content=f"## Your notes\n{wrap_untrusted('memory', body)}",
					include_in_memory=True)

			if params.action == "show":
				if params.note_id is None:
					return ActionResult(extracted_content="show requires note_id.", include_in_memory=True)
				note = await prov.note_get(user_id, params.note_id)
				if note is None:
					return ActionResult(extracted_content="Note not found.", include_in_memory=True)
				backs = []
				if note.get("title"):
					backs = await prov.note_backlinks(user_id, note["title"])
				body = (f"#{note['id']} {note['title'] or '(untitled)'} "
				        f"[{', '.join(note['tags'])}] status={note['status']} "
				        f"({_fmt_ts(note['created_ts'])})\n{note['content']}")
				if note["links"]:
					body += f"\nlinks: {', '.join('[[' + l + ']]' for l in note['links'])}"
				if backs:
					body += f"\nbacklinked from: {', '.join('#' + str(b['id']) + ' ' + (b['title'] or '') for b in backs)}"
				return ActionResult(
					extracted_content=f"## Note\n{wrap_untrusted('memory', body)}",
					include_in_memory=True)

			# ---- writes below: audited, threat-scanned, forged-turn-disciplined ----
			# T4-02: curated writes shape the agent's own future recall — emit a
			# first-class memory_write event so they are auditable (fail-open).
			def _memory_write_ev(op: str, ok: bool, removed: int = 0):
				try:
					from agents.task.telemetry.memory_events import emit_memory_event
					emit_memory_event(
						"memory_write", user_id=user_id or "",
						session_id=(getattr(execution_context, 'session_id', None)
						            or getattr(self, 'session_id', '') or ""),
						source="memory_tool", scope="curated",
						content=params.content or "", op=op, ok=ok, removed=removed)
				except Exception:
					pass

			_self_mod_ev = self_mod_emitter(
				execution_context, self, user_id,
				kind="note", source="memory_tool", created_by="agent")

			def _scan_blocked(*texts) -> bool:
				"""True when the write must be rejected. Fail-CLOSED like skill
				writes: a scanner error blocks the write (never persist unscanned)."""
				try:
					from modules.memory.task.threat_scan import is_suspicious
					return any(is_suspicious(t) for t in texts if t)
				except Exception:
					return True

			# SK-F10 discipline: a forged/autonomous turn (self-wake, delegation-result,
			# sub-agent, goal/cron) may only QUARANTINE new notes (status='pending',
			# owner reviews later) and may never mutate an active one.
			forged = _is_forged_or_autonomous_turn(execution_context, self)

			if params.action in ("add", "create"):
				if _scan_blocked(params.content, params.title):
					_memory_write_ev(params.action, False)
					return ActionResult(
						extracted_content="Rejected: note content failed the safety scan.",
						include_in_memory=True)
				status = "pending" if forged else "active"
				nid = await prov.note_create(
					user_id, params.content or "",
					title=params.title, tags=params.tags,
					source=f"session:{getattr(execution_context, 'session_id', None) or getattr(self, 'session_id', '') or ''}",
					created_by="background_review" if forged else "agent",
					status=status)
				ok = nid is not None
				_memory_write_ev(params.action, ok)
				if params.action == "create":
					_self_mod_ev("create", nid, ok=ok, pending=(status == "pending"))
				if not ok:
					return ActionResult(
						extracted_content="Could not save (empty, over size/entry cap, or no tenant).",
						include_in_memory=True)
				msg = (f"Saved note #{nid}." if status == "active"
				       else f"Saved note #{nid} as PENDING (this turn is autonomous/forged — an owner review promotes it).")
				return ActionResult(extracted_content=msg, include_in_memory=True)

			if params.action == "update":
				if forged:
					return ActionResult(
						extracted_content="Refused: an autonomous/forged turn cannot modify an existing note.",
						include_in_memory=True)
				if params.note_id is None:
					return ActionResult(extracted_content="update requires note_id.", include_in_memory=True)
				if _scan_blocked(params.content, params.title):
					_memory_write_ev("update", False)
					return ActionResult(
						extracted_content="Rejected: note content failed the safety scan.",
						include_in_memory=True)
				ok = await prov.note_update(
					user_id, params.note_id, content=params.content,
					title=params.title, tags=params.tags)
				_memory_write_ev("update", ok)
				_self_mod_ev("patch", params.note_id, ok=ok)
				return ActionResult(
					extracted_content=(f"Updated note #{params.note_id}." if ok
					                   else "Update failed (not found, empty, or over cap)."),
					include_in_memory=True)

			if params.action == "archive":
				if forged:
					return ActionResult(
						extracted_content="Refused: an autonomous/forged turn cannot archive a note.",
						include_in_memory=True)
				if params.note_id is None:
					return ActionResult(extracted_content="archive requires note_id.", include_in_memory=True)
				ok = await prov.note_archive(user_id, params.note_id)
				_memory_write_ev("archive", ok)
				_self_mod_ev("archive", params.note_id, ok=ok)
				return ActionResult(
					extracted_content=(f"Archived note #{params.note_id}." if ok
					                   else "Archive failed (not found)."),
					include_in_memory=True)

			# remove (legacy substring delete; owner-turn only like update/archive)
			if forged:
				return ActionResult(
					extracted_content="Refused: an autonomous/forged turn cannot remove notes.",
					include_in_memory=True)
			n = await prov.curated_remove(user_id, params.content or "")
			_memory_write_ev("remove", n > 0, removed=n)
			return ActionResult(
				extracted_content=f"Removed {n} curated memory entr{'y' if n == 1 else 'ies'}.",
				include_in_memory=True,
			)

	def _register_skill_manage_action(self):
		"""Register the writable-skills `skill_manage` tool (W2), gated SKILLS_WRITABLE.

		Lets the agent author/refine/retire durable skills. Default OFF. All safety is
		enforced in SkillWriterMixin (tenant-confined writes, validators, threat-scan,
		.pending/ quarantine, atomic write, archive-never-delete). A non-user-initiated
		turn can never auto-activate a skill (forced quarantine). Registered in this
		module — which deliberately omits `from __future__ import annotations` so the
		Registry can introspect the closure's first-param model.
		"""
		try:
			from core.config_policy import AutonomyConfig
			if not AutonomyConfig.skills_writable():
				return
		except Exception:
			return

		from typing import Literal as _Literal

		class SkillManageAction(BaseModel):
			action: _Literal["create", "patch", "delete", "promote"]
			skill_id: str
			content: Optional[str] = None        # create: full SKILL.md body
			old_string: Optional[str] = None     # patch: exact text to replace
			new_string: Optional[str] = None     # patch: replacement
			replace_all: bool = False
			description: str = ""
			expected_revision: Optional[str] = None  # promote/patch CAS token from pending review

		@self.registry.action(
			"Author your own durable SKILLS (procedures you can reload in future "
			"sessions). action='create' writes a new SKILL.md (markdown, starts with a "
			"# heading); action='patch' edits one by exact-string replace; "
			"action='delete' archives one; action='promote' activates a pending draft "
			"(owner-only — only the bound owner principal can promote; neither a "
			"background/sub-agent turn nor a non-owner turn can self-promote). New/edited "
			"skills are quarantined for review before they activate. Use to capture a "
			"reusable procedure you just worked out.",
			param_model=SkillManageAction,
		)
		async def skill_manage(params: SkillManageAction, execution_context=None) -> ActionResult:
			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
			if not user_id:
				return ActionResult(error="skill authoring requires a user (tenant scope).",
				                    include_in_memory=True)

			# T4-06: every effected skill mutation records a first-class
			# self_modification event (durable log → /telemetry + /activity). Fail-open.
			_self_mod_ev = self_mod_emitter(
				execution_context, self, user_id,
				kind="skill", source="skill_manage",
				item_id=params.skill_id, created_by="")
			# A forged (non-user-initiated) turn must never auto-activate a skill. The
			# RELIABLE signal is the execution context: the background-review reviewer
			# (and any delegated worker) runs as a sub-agent / leaf role — those fields
			# ARE set in _build_execution_context. A self-wake/delegation-result
			# re-entry into the MAIN agent is additionally detected via
			# `execution_context.metadata['turn_kind']` being one of `FORGED_TURN_KINDS`
			# (SK-F10, stamped live from the drained HITL message kind — see
			# `_is_forged_or_autonomous_turn` above). A sub-agent/leaf author → background_review (ALWAYS
			# quarantined); a normal interactive main-agent turn → agent (follows
			# SKILLS_WRITABLE_REQUIRE_REVIEW).
			from agents.task.agent.skill_writer import PROVENANCE_AGENT, PROVENANCE_BACKGROUND
			# C7: fold autonomous (goal/cron) runs into "forged" so they can't
			# auto-activate or self-promote a skill either.
			is_forged = _is_forged_or_autonomous_turn(execution_context, self)
			created_by = PROVENANCE_BACKGROUND if is_forged else PROVENANCE_AGENT
			try:
				from agents.task.agent.skill_manager import get_skill_manager
				sm = get_skill_manager()
				if params.action == "promote":
					# Owner-gated (T3-02): promoting a pending draft into ACTIVE content
					# is the entire security boundary of the writable-skills quarantine —
					# an active skill auto-activates in future sessions. A forged turn is
					# blocked, AND (mirroring self_context_manage promote, permissions
					# audit F4) a non-owner genuine turn is blocked too: otherwise the
					# agent could `create` (-> .pending under REQUIRE_REVIEW) then
					# `promote` in the SAME turn, activating a body the owner never saw —
					# e.g. an injected "author skill X and promote it" from fetched content.
					# is_owner_local_safe is the surface-independent owner check (a
					# forgeable network sender's uid is never the local tenant).
					owner_ok = False
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
							error="promote is owner-only; your pending draft awaits operator review.",
							include_in_memory=True)
					res = sm.promote_pending_skill(params.skill_id, user_id=user_id,
					                               description=params.description,
					                               expected_revision=params.expected_revision)
					if not res.ok:
						return ActionResult(error=f"promote failed: {'; '.join(res.errors)}",
						                    include_in_memory=True)
					_self_mod_ev("promote", pending=False, created_by="owner")
					return ActionResult(
						extracted_content=f"Skill `{params.skill_id}` promoted (active next session).",
						include_in_memory=True)
				if params.action == "create":
					if not params.content:
						return ActionResult(error="create requires `content`.", include_in_memory=True)
					res = sm.create_skill(params.skill_id, params.content, user_id=user_id,
					                      description=params.description, created_by=created_by)
				elif params.action == "patch":
					if params.old_string is None or params.new_string is None:
						return ActionResult(error="patch requires `old_string` and `new_string`.",
						                    include_in_memory=True)
					res = sm.patch_skill(params.skill_id, user_id=user_id,
					                     old_string=params.old_string, new_string=params.new_string,
					                     replace_all=params.replace_all, created_by=created_by,
					                     expected_revision=params.expected_revision)
				else:  # delete
					ok = sm.delete_skill(params.skill_id, user_id=user_id, created_by=created_by)
					if ok:
						_self_mod_ev("delete", created_by=created_by)
					return ActionResult(
						extracted_content=(f"Archived skill `{params.skill_id}`." if ok
						                   else f"No skill `{params.skill_id}` to archive."),
						include_in_memory=True,
					)
			except Exception as e:
				self.logger.debug(f"skill_manage failed: {e}")
				return ActionResult(error=f"skill_manage failed: {e}", include_in_memory=True)

			if not res.ok:
				return ActionResult(error=f"Skill rejected: {'; '.join(res.errors)}",
				                    include_in_memory=True)
			where = "pending review" if res.pending else "active"
			_self_mod_ev(params.action, pending=bool(res.pending), created_by=created_by)
			# §7.1: notify the owner when a skill lands in pending (fail-open, gated).
			if res.pending:
				try:
					from core import self_evolution as _se
					from core.instance import resolve_instance_id as _rii
					_cfg = getattr(getattr(self, 'container', None), 'config', None)
					_dd = data_dir_or_home(getattr(_cfg, 'data_dir', None))
					await _se.maybe_notify_owner_pending(
						getattr(self, 'container', None), user_id,
						home_dir=_dd, instance_id=_rii(), skill_manager=sm)
				except Exception as _e:
					self.logger.debug(f"self-evolution notify skipped: {_e}")
			return ActionResult(
				extracted_content=f"Skill `{params.skill_id}` saved ({where}).",
				include_in_memory=True,
			)

	def _register_message_action(self):
		"""Register the gated `message` action: resolve the target's tier
		(owner/allowlisted/denied) and route an approved send through the
		existing MessageRouter. Gated MESSAGE_TOOL_ENABLED (default OFF; ON
		under POLYROB_LOCAL). Registered in this module — no `from __future__
		import annotations` (registry-closure introspection)."""
		try:
			from core.config_policy import message_tool_enabled
			if not message_tool_enabled():
				return
		except Exception:
			return

		from tools.controller.message_send import perform_message_send
		from tools.controller.views import MessageTargetAction

		@self.registry.action(
			'Send a message — and any FILES you made — to a chat/recipient on a surface '
			'(telegram/email/whatsapp/discord/slack/signal/x). Attach with media_paths, '
			'one call per file; a path in text is NOT a delivery. Owner/allowlisted only.',
			param_model=MessageTargetAction,
		)
		async def message(params: MessageTargetAction, execution_context=None) -> ActionResult:
			# Forged/untrusted/autonomous turns must not reach ARBITRARY targets;
			# MESSAGE_AUTONOMOUS_ALLOWLISTED only forwards to the tier gate below.
			refusal = _autonomous_message_refusal(execution_context, self)
			if refusal is not None:
				return refusal

			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None) or ""
			container = getattr(self, "container", None)
			router = container.get_service("message_router") if container else None
			allowlist = container.get_service("outbound_allowlist") if container else None
			from tools.controller.message_send import prepare_message_targets
			owner_targets, surface, target = prepare_message_targets(container, user_id, params.surface, params.target)
			session_id = (getattr(execution_context, 'session_id', None)
			              or getattr(self, 'session_id', '') or "")

			# 2026-08-27 dedup-guard fix: refuse a repeat autonomous send to the
			# owner within the cooldown window (checked against the durable
			# conversation store, not the model's own elapsed-time claim).
			cooldown_refusal = _autonomous_owner_resend_cooldown_refusal(
				execution_context, self, container=container, user_id=user_id, surface=surface,
				target=target, owner_targets=owner_targets, text=params.text)
			if cooldown_refusal is not None:
				return cooldown_refusal

			res = await perform_message_send(
				router=router, allowlist=allowlist, owner_targets=owner_targets,
				user_id=user_id, surface=surface, target=target,
				text=params.text, action=params.action, reply_to=params.reply_to,
				message_id=params.message_id, media_paths=params.media_paths,
				session_id=session_id, container=container, execution_context=execution_context, controller=self)
			return _message_action_result(res, surface, target, params.text)

	def _register_contact_history_action(self):
		"""Register the read-only `contact_history` action (E3, 2026-07-13 review):
		"what did we already say to this address" — durable, address-keyed, survives
		session compaction/reset. Gated CORRESPONDENT_ACCESS_ENABLED. Output derives
		from correspondent text, so it is returned untrusted-wrapped. Registered in
		this module — no `from __future__ import annotations` (registry-closure
		introspection)."""
		try:
			from core.surfaces.config import SurfaceConfig
			if not SurfaceConfig.correspondent_access_enabled():
				return
		except Exception:
			return

		from tools.controller.views import ContactHistoryAction

		@self.registry.action(
			'Show the durable conversation history with an external correspondent '
			'(what we sent and what they replied, across sessions). Omit '
			'surface+address to LIST all conversations (who replied, who has not). '
			'Read-only.',
			param_model=ContactHistoryAction,
		)
		async def contact_history(params: ContactHistoryAction, execution_context=None) -> ActionResult:
			user_id = (getattr(execution_context, 'user_id', None)
			           or getattr(self, 'user_id', None) or "")
			container = getattr(self, "container", None)
			store = container.get_service("conversation_store") if container else None
			if store is None:
				return ActionResult(
					extracted_content="contact_history: no conversation store available",
					include_in_memory=True)
			try:
				limit = min(int(params.limit or 10), 50)
				if not (params.surface and params.address):
					listing = store.format_list(user_id, limit=limit)
					return ActionResult(extracted_content=(
						listing or "no recorded conversations for this tenant"),
						include_in_memory=True)
				ctx = store.format_context(user_id, params.surface, params.address,
				                           limit=limit)
			except Exception as e:
				return ActionResult(extracted_content=f"contact_history failed: {e}",
				                    include_in_memory=True)
			if not ctx:
				return ActionResult(extracted_content=(
					f"no recorded conversation with {params.surface}:{params.address}"),
					include_in_memory=True)
			from core.security.untrusted_wrap import wrap_untrusted
			return ActionResult(extracted_content=wrap_untrusted(
				f"{params.surface}:{params.address}", ctx), include_in_memory=True)

	def _register_preferences_action(self):
		"""Register the agent-callable `preferences` tool (owner-UX P2 T2), gated
		PREFS_TOOL_ENABLED (default OFF; ON under POLYROB_LOCAL).

		Lets the agent read/change the tenant's typed conversational preferences
		(``core.prefs.PREF_SCHEMA``) and propose durable operating-contract rules —
		the conversational front-end to what `/config` does in the CLI. Safety:

		- SAFE-sensitivity keys write immediately via ``write_preference`` (the
		  same writer `/config set` uses). GUARDED keys (budget/approval caps,
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
		- `contract_propose` follows the skill_manage/self_context_manage
		  create/patch idiom instead: it is NOT refused for a forged turn, but
		  `created_by` is forced to `PROVENANCE_BACKGROUND`, which makes the
		  RULES writer quarantine unconditionally (never active) — the owner
		  promotes with `/pending`. Since 035 P1-7 it records into `owner.md`,
		  so a GENUINE owner turn may bind it now (`OWNER_RULES_IMMEDIATE`); a
		  forged one still cannot, which is what makes that safe.
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
		try:
			from core.config_policy import prefs_tool_enabled
			if not prefs_tool_enabled():
				return
		except Exception:
			return

		from typing import Literal as _Literal

		class PreferencesAction(BaseModel):
			operation: _Literal["list", "get", "set", "explain", "contract_propose"]
			key: Optional[str] = Field(
				None, description="Setting name — REQUIRED for get/explain/set "
				"(e.g. key='TWITTER_ENABLED')")
			value: Optional[str] = Field(
				None, description="set only: new value (str; coerced per the key's type)")
			text: Optional[str] = Field(
				None, description="contract_propose ONLY: full operating-contract body. "
				"NOT used by explain/get/set — those take `key`.")

		@self.registry.action(
			"Manage your typed conversational preferences (approvals, budget caps, "
			"goal quotas, digest/delivery settings, reply style, session defaults) and "
			"propose durable operating rules. PREFER THIS over writing prose into an "
			"identity doc whenever the owner's instruction maps to a typed key "
			"(e.g. 'be brief' -> style.verbosity, 'write in German' -> style.language): "
			"a SAFE key applies immediately and is actually enforced at read time, "
			"while a prose note is only advisory. operation='list' shows every preference "
			"(effective value/source/applies); operation='get' with `key` shows one; "
			"operation='explain' with `key` shows the full provenance chain for ANY "
			"setting (preference or env flag) — use it to answer 'why is X off?'. "
			"operation='set' with `key`+`value` changes one — SAFE keys apply "
			"immediately (per their `applies` granularity: live/next-turn/next-session); "
			"GUARDED keys (budget/approval ceilings) are NEVER changed directly by you — "
			"`set` instead queues a proposal for the owner to review (see /pending). "
			"operation='contract_propose' with `text` records a durable operating "
			"rule/constraint in the owner's RULES doc (it binds immediately when the "
			"owner asks for it on this turn, otherwise it is queued for review).",
			param_model=PreferencesAction,
		)
		async def preferences(params: PreferencesAction, execution_context=None) -> ActionResult:
			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
			if not user_id:
				return ActionResult(error="preferences requires a user (tenant scope).",
				                    include_in_memory=True)

			_cfg = getattr(getattr(self, 'container', None), 'config', None)
			data_dir = data_dir_or_home(getattr(_cfg, 'data_dir', None))
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
						lines.append(f"  {key} = {value}   ({source}, applies: {spec.applies}){tag}")
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
						      "operation='explain', key='TWITTER_ENABLED'. "
						      "`text` is only for contract_propose.",
						include_in_memory=True)
				try:
					from core import config_service
					info = config_service.explain(params.key, user_id=user_id,
					                              home_dir=data_dir)
				except KeyError:
					return ActionResult(error=f"unknown setting: {params.key}",
					                    include_in_memory=True)
				out = [f"{info.key} = {info.effective}   ({info.source})",
				       f"namespace: {info.namespace} | kind: {info.kind} | "
				       f"applies: {info.applies} | enforcement: {info.enforcement}"]
				if info.description:
					out.append(info.description)
				if info.chain:
					out.append("provenance (highest wins):")
					out.extend(f"  {s.origin}: {s.value}" for s in info.chain)
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
				out = [f"{params.key} = {value}   ({source})", f"applies: {spec.applies}"]
				if spec.description:
					out.append(spec.description)
				if spec.sensitivity == SENSITIVITY_GUARDED:
					out.append("sensitivity: guarded — changing this queues a proposal "
					           "for owner review, it is never applied directly")
				return ActionResult(extracted_content="\n".join(out), include_in_memory=True)

			# set / contract_propose are the write operations. `is_forged` is computed
			# once and applied DIFFERENTLY per operation, because they have different
			# quarantine shapes:
			#   - `set` on a SAFE key writes the ACTIVE preferences.toml immediately —
			#     there is no pending/promote step to fall back on, so a forged turn
			#     is refused OUTRIGHT (owner must be in the loop before anything
			#     changes). A GUARDED key never writes active state either way.
			#   - `contract_propose` (035 P1-7: writes the RULES doc) is allowed to
			#     PROPOSE on a forged turn, just forced to PROVENANCE_BACKGROUND,
			#     which makes the writer quarantine unconditionally. Activation is
			#     decided by OWNER_RULES_IMMEDIATE + a genuine owner turn, not by
			#     a per-doc review flag.
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

				from core.prefs import write_preference, load_preferences
				ok, err = write_preference(data_dir, user_id, params.key, params.value,
				                           instance_id=instance_id)
				if not ok:
					return ActionResult(error=err, include_in_memory=True)
				_self_mod_ev("set", params.key, pending=False)
				coerced = load_preferences(data_dir, user_id, instance_id).get(params.key)
				return ActionResult(
					extracted_content=f"Set {params.key} = {coerced} (applies: {spec.applies}).",
					include_in_memory=True)

			# contract_propose — 035 P1-7: FOLDED INTO RULES. Body (and the why) in
			# `doc_authoring.perform_rules_propose`; no NEW contract.md is created.
			if not params.text:
				return ActionResult(error="contract_propose requires `text`.",
				                    include_in_memory=True)
			from tools.controller.doc_authoring import perform_rules_propose
			res, result = perform_rules_propose(
				params.text, user_id=user_id, data_dir=data_dir,
				instance_id=instance_id, is_forged=is_forged)
			if res.ok:
				_self_mod_ev("contract_propose", user_id, pending=bool(res.pending),
				            created_by=("background_review" if is_forged else "agent"))
			return result

	def _register_mcp_install_action(self):
		"""Register the agent-callable `mcp_install` action (T3-01/W4-1).

		Wires the previously-orphaned tools/mcp/self_install.py::perform_mcp_install
		pipeline (gate → allowlist → screen → approve → add_server → persist).
		Safety posture:
		- action only registered when MCP_SELF_INSTALL_ENABLED=true (default OFF);
		- a forged/leaf/autonomous turn can never install (owner in the loop);
		- the approver is resolved EXPLICITLY: Deny-by-default unless the operator
		  set APPROVAL_PROVIDER — the enable flag alone never silently auto-approves;
		- only NAMED, reviewed catalog entries install (builtins + the operator's
		  MCP_INSTALL_CATALOG_FILE, T3-03) — never an agent-written config;
		- persisted via the tenant-scoped user_mcp_service row (T3-04), NEVER the
		  global config/mcp_config.json; stdio entries stay session-only;
		- direct {server}_{tool} actions re-registered post-install (T3-05) so the
		  new tools are callable the SAME session;
		- every attempt writes an event_log audit row.
		Registered in this module — no `from __future__ import annotations`
		(registry-closure introspection).
		"""
		try:
			from tools.mcp.self_install import self_install_enabled
			if not self_install_enabled():
				return
		except Exception:
			return

		from typing import Literal as _Literal

		class MCPInstallAction(BaseModel):
			action: _Literal["list", "install"] = "install"
			server_id: Optional[str] = None

		@self.registry.action(
			"Install a vetted MCP server from the reviewed catalog to gain new tools "
			"this session. action='list' shows the installable catalog; "
			"action='install' with server_id installs one (owner approval required). "
			"Only named catalog entries can be installed — never an arbitrary config.",
			param_model=MCPInstallAction,
		)
		async def mcp_install(params: MCPInstallAction, execution_context=None) -> ActionResult:
			import os as _os

			from tools.mcp.catalog import MCPCatalog
			from tools.mcp.self_install import perform_mcp_install

			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None) or ""
			session_id = (getattr(execution_context, 'session_id', None)
			              or getattr(self, 'session_id', '') or "")
			catalog = MCPCatalog()

			def _audit(server_id: str, outcome: str, **extra):
				try:
					from core.event_log import event_log_enabled, get_event_log
					if event_log_enabled():
						get_event_log().record(
							"mcp_install", user_id=user_id, session_id=session_id,
							source="mcp_install", server_id=server_id, outcome=outcome,
							**extra)
				except Exception:
					pass

			if params.action == "list":
				lines = ["Installable MCP servers (reviewed catalog):"]
				for sid in catalog.ids():
					e = catalog.get(sid)
					lines.append(f"- {sid} [{e.trust}] ({e.transport}): {e.description}")
				return ActionResult(extracted_content="\n".join(lines), include_in_memory=True)

			if not params.server_id:
				return ActionResult(error="install requires `server_id` (see action='list').",
				                    include_in_memory=True)

			# Owner in the loop: a self-wake/delegated/autonomous turn never installs.
			if _is_forged_or_autonomous_turn(execution_context, self):
				_audit(params.server_id, "refused_forged_turn")
				return ActionResult(
					extracted_content=(
						"mcp_install: not permitted for forged/autonomous turns — ask the "
						"owner to install it (the owner must be in the loop for new tools)."),
					include_in_memory=True)

			# The MCP subsystem must be loaded this session to host the new server.
			mcp_info = (self._tools or {}).get("mcp")
			mcp_tool = getattr(mcp_info, "instance", None) if mcp_info else None
			server_manager = getattr(mcp_tool, "server_manager", None) if mcp_tool else None
			if server_manager is None:
				_audit(params.server_id, "refused_no_mcp_tool")
				return ActionResult(
					error="mcp_install: the mcp tool is not loaded this session "
					      "(tool_ids must include 'mcp').",
					include_in_memory=True)

			# Explicit approver: Deny-by-default unless the operator opted into a
			# provider — MCP_SELF_INSTALL_ENABLED alone never silently auto-approves.
			from tools.controller.approval import (
				DenyByDefaultApprover, get_approval_provider_or_deny)
			_prov_name = (_os.getenv("APPROVAL_PROVIDER") or "").strip()
			provider = (get_approval_provider_or_deny(_prov_name)
			            if _prov_name else DenyByDefaultApprover())

			ok, msg = await perform_mcp_install(
				params.server_id,
				catalog=catalog,
				server_manager=server_manager,
				approve=provider.request,
				persist=None,  # tenant-scoped persist handled below (T3-04)
				context=execution_context,
			)
			if not ok:
				_audit(params.server_id, "failed", reason=msg)
				return ActionResult(error=msg, include_in_memory=True)

			# T3-05: surface the new server's tools as direct actions THIS session.
			try:
				await self._register_mcp_tools_as_direct_actions(mcp_tool)
			except Exception as e:
				self.logger.debug(f"mcp_install: direct-action re-register skipped: {e}")

			# T3-04: persist tenant-scoped (url transports only — user_mcp_service
			# blocks stdio by design; a stdio entry stays session-only).
			persisted = False
			entry = catalog.get(params.server_id)
			try:
				ums = (self.container.get_service("user_mcp_service")
				       if getattr(self, "container", None) else None)
				if ums is not None and entry is not None and entry.url:
					res = await ums.add_server(
						user_id, params.server_id, entry.url,
						server_type=entry.transport if entry.transport in ("sse", "http") else "sse",
						display_name=entry.description or params.server_id,
					)
					persisted = bool(getattr(res, "success", False))
			except Exception as e:
				self.logger.debug(f"mcp_install: tenant persist skipped: {e}")

			_audit(params.server_id, "installed", persisted=persisted)
			tail = " Persisted for future sessions." if persisted else " Session-only (not persisted)."
			return ActionResult(extracted_content=msg + tail, include_in_memory=True)

	def _register_insights_action(self):
		"""Delegates to tools/controller/insights_action.py (ratchet extraction)."""
		from tools.controller.insights_action import register_insights_action
		register_insights_action(self)

	def _register_agent_status_action(self):
		"""Delegates to tools/controller/agent_status_action.py (ratchet extraction,
		2026-08-28 status SSOT: the action renders from core/status_snapshot.py)."""
		from tools.controller.agent_status_action import register_agent_status_action
		register_agent_status_action(self)

	def _register_usage_summary_action(self):
		"""Register the read-only `usage_summary` action (Task 13, Phase 3 R3 —
		the metering-to-invoice bridge), gated USAGE_INVOICE_BRIDGE_ENABLED
		(default OFF; deliberately NOT in _SAFE_LOCAL_FLAGS — this is an explicit
		billing feature).

		Reports the tenant-scoped usage rollup (`modules.credits.usage_rollup`,
		which fixes G-29: `LLMUsageTracker.get_session_breakdown` aggregates by
		session_id ALONE with no user_id filter) plus a non-binding
		`suggested_invoice` draft. This action NEVER creates a payment request —
		`build_invoice_draft` never touches `create_payment_request` — the agent
		must still call the separate, approval-gated `x402_request` action itself
		to actually send an invoice.
		"""
		try:
			from modules.credits.usage_rollup import usage_invoice_bridge_enabled
			if not usage_invoice_bridge_enabled():
				return
		except Exception:
			return

		class UsageSummaryAction(BaseModel):
			session_id: Optional[str] = None
			since: Optional[str] = None

		@self.registry.action(
			"Show your tenant-scoped LLM usage (api cost / credits / calls), "
			"optionally narrowed to a session_id or a since timestamp, plus a "
			"non-binding suggested invoice draft. Read-only — never sends money; "
			"call x402_request yourself to actually create an invoice.",
			param_model=UsageSummaryAction,
		)
		async def usage_summary(params: UsageSummaryAction, execution_context=None) -> ActionResult:
			user_id = (getattr(execution_context, 'user_id', None)
			           or getattr(self, 'user_id', None) or "")
			if not user_id:
				return ActionResult(
					error=(
						"usage_summary refused: this action requires an authenticated "
						"tenant — the execution context has no user_id (anonymous "
						"financial state is refused)"
					),
					include_in_memory=True,
				)
			from modules.credits.usage_rollup import build_invoice_draft, usage_rollup
			rollup = await usage_rollup(user_id, session_id=params.session_id, since=params.since)
			draft = build_invoice_draft(rollup)

			scope = ""
			if params.session_id:
				scope += f", session {params.session_id}"
			if params.since:
				scope += f", since {params.since}"
			lines = [
				f"Usage summary (tenant {user_id}{scope}):",
				f"  api_cost_usd: ${rollup['api_cost_usd']:.4f}",
				f"  credits:      {rollup['credits']:.2f}",
				f"  calls:        {rollup['calls']}",
				"",
				f"Suggested invoice (NOT sent — call x402_request yourself): "
				f"${draft['amount_usd']:.2f} — {draft['purpose']}",
			]
			if draft["over_cap"]:
				lines.append(f"  WARNING: {draft['note']}")
			return ActionResult(
				extracted_content="\n".join(lines),
				metadata={"rollup": rollup, "suggested_invoice": draft},
				include_in_memory=True,
			)

	def _register_subtask_action(self):
		"""Register subtask action for sub-agent delegation.

		Allows the main agent to spawn sub-agents for specialized tasks.
		Only registers actions when sub-agents are enabled to prevent
		leaking sub-agent instructions to the LLM when disabled.
		"""
		# Gate registration on sub-agents being enabled
		# This prevents the LLM from seeing subtask tools when disabled
		from agents.task.constants import TimeoutConfig
		if not TimeoutConfig.get_sub_agents_enabled():
			self.logger.debug("Sub-agents disabled - skipping subtask action registration")
			return

		from tools.controller.views import SubtaskAction, ParallelSubtasksAction, DelegateTaskAction
		from tools.controller.delegation import evaluate_delegation

		# UP-10 2.4: single gated delegation core. subtask / parallel_subtasks /
		# delegate_task all route through _delegate so the role+depth gate
		# (evaluate_delegation) is enforced for ALL three verbs — previously only
		# delegate_task ran the gate, so a leaf could still delegate via subtask.
		async def _delegate(*, goal=None, tasks=None, role="leaf", profile="executor",
		                     max_steps=30, background=False, model=None, provider=None,
		                     execution_context=None, label="delegate_task") -> ActionResult:
			"""Gated delegation dispatch. Provide goal XOR tasks (list of SubtaskAction).

			UP-12: background=True (goal-only) detaches the child and returns immediately;
			its result re-enters the session as a new turn when it finishes.

			B4: optional per-sub-agent `model`/`provider` builds an isolated child LLM
			instead of inheriting the parent's. Goal shape: `model` applies to the one
			spawned child. Tasks shape: each SubtaskAction's own `model` wins; a task
			without one falls back to the top-level `model` (else inherits the parent's
			LLM, unchanged). Not supported together with `background=true` in v1 — the
			async delegation registry doesn't yet thread a model through, so that
			combination is rejected before dispatch rather than silently ignored.
			"""
			from agents.task.constants import TimeoutConfig

			# B4: `provider` only has meaning paired with a `model`. Reject a
			# provider given WITHOUT a model (top level, or on any parallel task)
			# with a clear error instead of silently discarding it.
			if provider and not model:
				return ActionResult(
					error="`provider` requires `model` — a provider without a model is "
					      "discarded, so this is rejected. Set both, or neither.",
					include_in_memory=True,
				)
			if tasks:
				for _i, _st in enumerate(tasks):
					if getattr(_st, "provider", None) and not getattr(_st, "model", None):
						return ActionResult(
							error=f"task[{_i}] sets `provider` without `model` — a provider "
							      "without a model is discarded. Set both, or neither.",
							include_in_memory=True,
						)

			caller_is_sub = execution_context.is_sub_agent if execution_context else False
			# H7: fail closed to the least-privileged role — a delegation call with no
			# context (or a context missing role) must NOT inherit orchestrator privilege.
			caller_role = getattr(execution_context, 'role', 'leaf') if execution_context else 'leaf'

			decision = evaluate_delegation(
				enabled=TimeoutConfig.get_sub_agents_enabled(),
				caller_is_sub_agent=caller_is_sub,
				caller_role=caller_role,
				requested_child_role=role,
				max_depth=TimeoutConfig.get_max_sub_agent_depth(),
			)
			if not decision.allowed:
				self.logger.info(f"🚫 {label} denied: {decision.reason}")
				return ActionResult(error=decision.reason, include_in_memory=True)

			# Resolve parent agent id from context (fallback to first active agent)
			parent_agent_id = None
			if execution_context and getattr(execution_context, 'agent_id', None):
				parent_agent_id = execution_context.agent_id
			elif getattr(self, 'orchestrator', None) and getattr(self.orchestrator, 'agents', None):
				parent_agent_id = list(self.orchestrator.agents.keys())[0]
			if not parent_agent_id:
				return ActionResult(error="Cannot delegate: no parent agent context", include_in_memory=True)

			mgr = getattr(self.orchestrator, 'sub_agent_manager', None) if getattr(self, 'orchestrator', None) else None
			if not mgr:
				return ActionResult(error="Sub-agent system not initialized", include_in_memory=True)

			# B4: background delegation doesn't (yet) thread a model override through the
			# async delegation registry — reject cleanly before dispatch rather than
			# silently ignoring the requested model.
			if background and model:
				return ActionResult(
					error="model override not yet supported for background delegation "
					      "(background=true always inherits the parent's model)",
					include_in_memory=True,
				)

			# UP-12: background dispatch (goal-only) — detach and return immediately.
			if background and goal:
				registry = getattr(self.orchestrator, 'async_delegation', None) if getattr(self, 'orchestrator', None) else None
				if registry is None:
					return ActionResult(error="Background delegation not available in this session", include_in_memory=True)
				deleg = await registry.dispatch(
					goal=goal, profile=profile, max_steps=max_steps,
					parent_agent_id=parent_agent_id, caller_is_sub=caller_is_sub,
				)
				if deleg.get("status") == "rejected":
					return ActionResult(error=deleg["error"], include_in_memory=True)
				return ActionResult(
					extracted_content=(
						f"## Background task dispatched 🛫\n\n"
						f"**delegation_id:** {deleg['delegation_id']}\n**goal:** {goal}\n\n"
						"The task is running in the background; its result will arrive as a new "
						"message when it finishes. Continue with other work."
					),
					include_in_memory=True,
				)

			def _resolve_parent_agent():
				"""Look up the parent Agent object (for building a child LLM). Mirrors
				the dual lookup pattern used elsewhere (plain id, then id_sessionid)."""
				orch = getattr(self, 'orchestrator', None)
				agents = getattr(orch, 'agents', None) if orch else None
				if not agents:
					return None
				session_id = getattr(orch, 'session_id', '')
				for lookup_id in (parent_agent_id, f"{parent_agent_id}_{session_id}"):
					if lookup_id in agents:
						return agents[lookup_id]
				return None

			async def _build_child_llm(m, p):
				"""Build an isolated child LLM for model `m` (+ optional provider `p`).

				Returns (llm, error_message) — error_message is None on success.
				Isolated so the child never shares the parent's cached HTTP client.
				"""
				parent_agent = _resolve_parent_agent()
				build_fn = getattr(parent_agent, '_create_llm_from_config_async', None) if parent_agent else None
				if build_fn is None:
					return None, f"Could not build sub-agent model '{m}': parent agent unavailable"
				cfg = {"model": m}
				if p:
					cfg["provider"] = p
				try:
					child_llm = await build_fn(cfg, isolated=True)
				except Exception as e:
					self.logger.error(f"{label}: failed to build child LLM for '{m}': {e}", exc_info=True)
					return None, f"Could not build sub-agent model '{m}': {e}"
				if child_llm is None:
					return None, f"Could not build sub-agent model '{m}'"
				return child_llm, None

			try:
				if goal:
					child_llm = None
					if model:
						child_llm, build_err = await _build_child_llm(model, provider)
						if build_err:
							return ActionResult(error=build_err, include_in_memory=True)

					self.logger.info(f"🚀 {label} (goal): {goal[:100]}... (profile: {profile})")
					result = await mgr.run_subtask(
						task=goal,
						parent_agent_id=parent_agent_id,
						profile_id=profile,
						max_steps=max_steps,
						parent_llm=child_llm,
						is_parent_sub_agent=caller_is_sub,
					)
					if result.success:
						return ActionResult(
							extracted_content=(
								f"## Delegated goal completed ✅\n\n**Goal:** {goal}\n\n"
								f"**Result:**\n{result.output}"
							),
							include_in_memory=True,
						)
					return ActionResult(error=f"Delegated goal failed: {result.error}", include_in_memory=True)

				# tasks shape -> parallel. Per-task `model` wins; a task without its own
				# falls back to the top-level `model` (if set), else inherits the parent's
				# LLM unchanged. All required child LLMs are built BEFORE dispatch so a
				# bad model name aborts the whole call rather than a silent partial run.
				self.logger.info(f"🚀 {label} (parallel): {len(tasks)} tasks")
				subtask_dicts = []
				for st in tasks:
					st_model = st.model or model
					st_provider = st.provider if st.model else provider
					st_llm = None
					if st_model:
						st_llm, build_err = await _build_child_llm(st_model, st_provider)
						if build_err:
							return ActionResult(error=build_err, include_in_memory=True)
					subtask_dicts.append({
						'task': st.task, 'profile': st.profile, 'max_steps': st.max_steps,
						'llm': st_llm,
					})
				results = await mgr.run_parallel_subtasks(
					subtasks=subtask_dicts,
					parent_agent_id=parent_agent_id,
				)
				output = mgr.format_results_for_prompt(results)
				successful = sum(1 for r in results if r.success)
				return ActionResult(
					extracted_content=(
						f"## Delegated tasks complete\n\n"
						f"**Results:** {successful}/{len(results)} succeeded\n\n{output}"
					),
					include_in_memory=True,
				)
			except Exception as e:
				self.logger.error(f"{label} failed: {e}", exc_info=True)
				return ActionResult(error=f"{label} error: {str(e)}", include_in_memory=True)

		# Expose the gated core so UP-12 (background delegation) and tests can reach it.
		self._delegate_core = _delegate
		# One-shot deprecation notices for the legacy verbs.
		_deprecation_warned = {"subtask": False, "parallel_subtasks": False}
		@self.registry.action(
			'Delegate a subtask to a sub-agent for focused execution '
			'(deprecated alias of delegate_task; use delegate_task with goal=...)',
			param_model=SubtaskAction
		)
		async def subtask(params: SubtaskAction, execution_context=None) -> ActionResult:
			"""Deprecated alias — forwards into the gated delegate_task core."""
			if not _deprecation_warned["subtask"]:
				_deprecation_warned["subtask"] = True
				self.logger.info("subtask is deprecated; use delegate_task (goal=...)")
			return await _delegate(
				goal=params.task, profile=params.profile, max_steps=params.max_steps,
				execution_context=execution_context, label="subtask",
			)

		@self.registry.action(
			'Run multiple subtasks in parallel using sub-agents '
			'(deprecated alias of delegate_task; use delegate_task with tasks=[...])',
			param_model=ParallelSubtasksAction
		)
		async def parallel_subtasks(params: ParallelSubtasksAction, execution_context=None) -> ActionResult:
			"""Deprecated alias — forwards into the gated delegate_task core."""
			if not _deprecation_warned["parallel_subtasks"]:
				_deprecation_warned["parallel_subtasks"] = True
				self.logger.info("parallel_subtasks is deprecated; use delegate_task (tasks=[...])")
			return await _delegate(
				tasks=params.subtasks, execution_context=execution_context, label="parallel_subtasks",
			)
		# Unified Reference-style delegation surface (roadmap P1): one ergonomic tool
		# (goal XOR tasks) with an explicit role/depth gate.
		@self.registry.action(
			'Delegate work to sub-agent(s): one focused "goal", or 2-5 parallel "tasks". '
			'Expensive. Synchronous by default (blocks this turn until done). Set '
			'background=true (goal only) to run it detached and get the result as a new '
			'message later — durable across the turn but NOT across restart (use the '
			'scheduler for durable scheduled work). Set "model" (and optionally '
			'"provider") to run the sub-agent on a different model than yours — e.g. a '
			'cheaper/faster model for a simple task; omit to inherit your model. With '
			'"tasks", each task can set its own "model", falling back to the top-level '
			'"model" if unset. Not supported together with background=true.',
			param_model=DelegateTaskAction
		)
		async def delegate_task(params: DelegateTaskAction, execution_context=None) -> ActionResult:
			"""Spawn sub-agent(s) for delegated work, gated by role + depth."""
			return await _delegate(
				goal=params.goal, tasks=params.tasks, role=params.role,
				profile=params.profile, max_steps=params.max_steps,
				background=params.background, model=params.model, provider=params.provider,
				execution_context=execution_context, label="delegate_task",
			)

	def _register_backward_compat_aliases(self):
		"""Register backward compatibility aliases for renamed task actions.

		This allows LLMs to use old todo action names (todo_list, todo_add, etc.)
		which automatically map to new namespaced names (task_todo_list, task_todo_add, etc.).

		NOTE: 'done' is NOT aliased - it's a core Controller action, not part of TaskTool.
		"""
		aliases = {
			# Todo action aliases (old -> new)
			'todo_list': 'task_todo_list',
			'todo_add': 'task_todo_add',
			'todo_complete': 'task_todo_complete',
			'todo_progress': 'task_todo_progress',
			'todo_next': 'task_todo_next',
			# NOTE: 'done' is NOT here - it's a native Controller action
		}

		for old_name, new_name in aliases.items():
			# Use thread-safe create_alias method from Registry
			if self.registry.create_alias(old_name, new_name):
				self.logger.debug(f"Created backward compat alias: {old_name} -> {new_name}")
			else:
				# Don't warn if task tool not loaded yet - just skip
				self.logger.debug(f"Skipping alias {old_name} -> {new_name}: target not registered yet")
