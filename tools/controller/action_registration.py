"""ActionRegistrationMixin — Controller one-shot action registration (UP-11, verbatim
code-motion). Registers send_message/done/load_skill/session_search/memory/delegation
closures + backward-compat aliases. Methods use ``self`` (the composed Controller)
exactly as before via the MRO."""
import os
from typing import Any, Dict, List, Optional

from core.env import bool_env as _bool_env

from pydantic import BaseModel

from tools.controller.types import ActionResult
from tools.controller.views import DoneAction, SendMessageAction
from tools.controller._helpers import build_load_skill_result, read_skill_resource_confined
from modules.llm.messages import AIMessage
from agents.task.agent.core.self_wake import FORGED_TURN_KINDS as _FORGED_TURN_KINDS


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
		return False


class ActionRegistrationMixin:
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
						_mirror = build_discrete_publish(
							getattr(self.orchestrator, "_message_router", None),
							getattr(self.orchestrator, "_chat_session_key", None),
						)
						await _mirror(params.text)
					except Exception as e:
						self.logger.debug(f"Could not mirror message to router: {e}")

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
					return ActionResult(
						extracted_content=f"Message sent to user. Task paused - will resume when user responds.",
						include_in_memory=True,
						is_done=True  # Stop execution, wait for user via continuous chat
					)

				# Non-blocking message sent.
				# R1: tag it so the run loop can recognize a turn whose only output was a
				# user-facing reply (no productive tool work) and end it — otherwise a
				# chat reply loops and re-greets, since non-blocking sends keep is_done False.
				return ActionResult(
					extracted_content=f"Message sent to user (non-blocking)",
					include_in_memory=False,
					metadata={'conversational_reply': True}
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
					try:
						from core.surfaces.outbound_mirror import build_discrete_publish
						_mirror = build_discrete_publish(
							getattr(self.orchestrator, "_message_router", None),
							getattr(self.orchestrator, "_chat_session_key", None),
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
						if _skill_manager.resolve_skill_dir(sid, uid) is not None:
							content = _skill_manager._load_skill_content(sid, user_id=uid)
						if content:
							from agents.task.agent.skill_manager import MatchedSkill
							fallback_skill = MatchedSkill(
								skill_id=sid,
								priority=0,
								match_reasons=["disk-fallback"],
								content=content,
							)
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

		# W7: optional read-only insights tool (authored-skill reuse %), gated
		# INSIGHTS_TOOL (default false). Tenant-scoped.
		self._register_insights_action()

		# Ensure normalize_path method exists for file operations
		self._ensure_normalize_path_exists()

		# Task 5: gated `message` action (owner/allowlist -> MessageRouter send),
		# gated MESSAGE_TOOL_ENABLED (default false; ON under POLYROB_LOCAL).
		self._register_message_action()

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

		_SEARCH_DESC = (
			"Recall your durable memory of PAST sessions. WHEN TO USE: reach for this "
			"BEFORE web/filesystem on 'what did we do about X', 'where did we leave Y', "
			"or 'what was I working on'. Two shapes: pass a `query` to DISCOVER relevant "
			"past work (facts, decisions); leave `query` empty to BROWSE your most-recent "
			"sessions. Optional `limit` (1-20) and `sort` ('newest'/'oldest'). "
			"Optional `collection`: set to search a named knowledge-base collection instead "
			"of past sessions (requires KB to be enabled)."
		)

		def _kb_on() -> bool:
			try:
				from agents.task.constants import AutonomyConfig
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
						from agents.task.agent.core.untrusted_wrap import wrap_untrusted
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
					limit=params.limit, sort=params.sort,
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
				from agents.task.agent.core.untrusted_wrap import wrap_untrusted
				recalled = wrap_untrusted("session_search", recalled)
			except Exception:
				pass  # fail-open: never block recall on a wrap import error
			header = ("## Recalled from past sessions" if params.query
			          else "## Your most-recent sessions")
			return ActionResult(
				extracted_content=f"{header}\n{recalled}",
				include_in_memory=True,
			)

		@self.registry.action(_SEARCH_DESC, param_model=SessionSearchAction)
		async def session_search(params: SessionSearchAction, execution_context=None) -> ActionResult:
			return await _run_session_search(params, execution_context)

		# W6: Reference exposes the same recall under both names. Register `memory_search`
		# as a thin alias (default-on via MEMORY_SEARCH_TOOL) so prompts/tools that use
		# either shape resolve. Same handler, same tenant scoping — zero new surface.
		try:
			from agents.task.constants import AutonomyConfig
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
			from agents.task.constants import AutonomyConfig
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
				from agents.task.agent.core.untrusted_wrap import wrap_untrusted
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
			action: _Literal["read", "add", "remove"]
			content: Optional[str] = None   # required for add (entry) / remove (substring)

		@self.registry.action(
			"Your durable, curated long-term notes (separate from automatic session "
			"recall). action='add' to save a short fact worth remembering across "
			"sessions; action='read' to list your notes; action='remove' to delete "
			"notes containing a substring. Keep entries short and factual.",
			param_model=MemoryToolAction,
		)
		async def memory(params: MemoryToolAction, execution_context=None) -> ActionResult:
			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
			from modules.memory.registry import get_memory_registry
			prov = get_memory_registry().active()
			if prov is None or not hasattr(prov, 'curated_add'):
				return ActionResult(extracted_content="Memory tool unavailable.", include_in_memory=False)
			if params.action == "read":
				notes = await prov.curated_read(user_id)
				return ActionResult(
					extracted_content=(f"## Your curated memory\n{notes}" if notes.strip()
					                    else "Your curated memory is empty."),
					include_in_memory=True,
				)
			if params.action == "add":
				ok = await prov.curated_add(user_id, params.content or "")
				return ActionResult(
					extracted_content=("Saved to curated memory." if ok
					                   else "Could not save (empty, over size/entry cap, or no tenant)."),
					include_in_memory=True,
				)
			# remove
			n = await prov.curated_remove(user_id, params.content or "")
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
			from agents.task.constants import AutonomyConfig
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

		@self.registry.action(
			"Author your own durable SKILLS (procedures you can reload in future "
			"sessions). action='create' writes a new SKILL.md (markdown, starts with a "
			"# heading); action='patch' edits one by exact-string replace; "
			"action='delete' archives one; action='promote' activates a pending draft "
			"(owner-only — a background/sub-agent turn cannot self-promote). New/edited "
			"skills are quarantined for review before they activate. Use to capture a "
			"reusable procedure you just worked out.",
			param_model=SkillManageAction,
		)
		async def skill_manage(params: SkillManageAction, execution_context=None) -> ActionResult:
			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
			if not user_id:
				return ActionResult(error="skill authoring requires a user (tenant scope).",
				                    include_in_memory=True)
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
					# Owner-gated: a forged (sub-agent/leaf/background/autonomous) turn
					# must NEVER self-promote its own pending draft into active content.
					if is_forged:
						return ActionResult(
							error="promote is owner-only; your pending draft awaits operator review.",
							include_in_memory=True)
					res = sm.promote_pending_skill(params.skill_id, user_id=user_id,
					                               description=params.description)
					if not res.ok:
						return ActionResult(error=f"promote failed: {'; '.join(res.errors)}",
						                    include_in_memory=True)
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
					                     replace_all=params.replace_all, created_by=created_by)
				else:  # delete
					ok = sm.delete_skill(params.skill_id, user_id=user_id, created_by=created_by)
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
			# §7.1: notify the owner when a skill lands in pending (fail-open, gated).
			if res.pending:
				try:
					from core import self_evolution as _se
					from core.instance import resolve_instance_id as _rii
					_cfg = getattr(getattr(self, 'container', None), 'config', None)
					_dd = getattr(_cfg, 'data_dir', 'data') or 'data'
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
			from agents.task.constants import message_tool_enabled
			if not message_tool_enabled():
				return
		except Exception:
			return

		from tools.controller.message_send import perform_message_send
		from tools.controller.views import MessageTargetAction

		@self.registry.action(
			'Send a message to a specific chat/recipient on a given surface '
			'(telegram/email/whatsapp). Only the owner and owner-allowlisted targets '
			'are permitted; other targets are denied.',
			param_model=MessageTargetAction,
		)
		async def message(params: MessageTargetAction, execution_context=None) -> ActionResult:
			import os
			from core.instance import resolve_owner_telegram_id, resolve_owner_email

			# Forged/untrusted/autonomous turns must not reach arbitrary targets
			# (sub-agent, self-wake/delegation-result re-entry into the main agent,
			# or an autonomous goal/cron/planner-spawned session).
			if _is_forged_or_autonomous_turn(execution_context, self):
				return ActionResult(
					extracted_content=(
						"message: not permitted for forged/autonomous turns "
						"(owner must be in the loop)"),
					include_in_memory=True)

			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None) or ""
			container = getattr(self, "container", None)
			router = container.get_service("message_router") if container else None
			allowlist = container.get_service("outbound_allowlist") if container else None

			owner_targets = {}
			tid = resolve_owner_telegram_id(os.environ)
			if tid:
				owner_targets["telegram"] = str(tid)
			oem = resolve_owner_email(os.environ)
			if oem:
				owner_targets["email"] = oem

			res = await perform_message_send(
				router=router, allowlist=allowlist, owner_targets=owner_targets,
				user_id=user_id, surface=params.surface, target=params.target,
				text=params.text, action=params.action, reply_to=params.reply_to,
				message_id=params.message_id)
			return ActionResult(extracted_content=(
				f"message[{res['tier']}] -> {params.surface}:{params.target} "
				f"{'OK' if res['success'] else 'FAILED: ' + (res.get('error') or '')}"),
				include_in_memory=True)

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
			from agents.task.constants import AutonomyConfig
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

		@self.registry.action(
			"Refine your evolving SELF context — durable notes about how you work with "
			"THIS user (preferences, conventions, what you've learned). action='read' "
			"returns the current text; action='update' replaces it (≤2200 chars — "
			"consolidate, don't sprawl); action='patch' edits by exact-string replace; "
			"action='promote' activates your pending draft (owner-only). Updates/patches "
			"are QUARANTINED for review and apply next session. This is NOT your core "
			"identity/boundaries (those are operator-owned).",
			param_model=SelfContextManageAction,
		)
		async def self_context_manage(params: SelfContextManageAction, execution_context=None) -> ActionResult:
			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
			if not user_id:
				return ActionResult(error="self-context requires a user (tenant scope).",
				                    include_in_memory=True)
			# Resolve the instance home dir (same as construction).
			_cfg = getattr(getattr(self, 'container', None), 'config', None)
			data_dir = getattr(_cfg, 'data_dir', 'data') or 'data'
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
					from agents.task.constants import local_mode_enabled
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
				return ActionResult(extracted_content="Self-context promoted (active next session).",
				                    include_in_memory=True)

			# update / patch: ALWAYS quarantine to .pending (pending=True below) — the
			# action never writes the active doc directly; activation is the owner-gated
			# `promote` above. `created_by` still reflects real forged status so the
			# writer additionally bars a forged turn from even reading/patching an active
			# doc, while a normal turn may patch the active doc INTO a pending edit.
			created_by = PROVENANCE_BACKGROUND if is_forged else PROVENANCE_AGENT
			try:
				if params.action == "update":
					if not params.content:
						return ActionResult(error="update requires `content`.", include_in_memory=True)
					res = writer.propose(params.content, user_id=user_id, created_by=created_by,
					                     pending=True)
				else:  # patch
					if params.old_string is None or params.new_string is None:
						return ActionResult(error="patch requires `old_string` and `new_string`.",
						                    include_in_memory=True)
					res = writer.patch(user_id=user_id, old_string=params.old_string,
					                   new_string=params.new_string, replace_all=params.replace_all,
					                   created_by=created_by, pending=True)
			except Exception as e:
				self.logger.debug(f"self_context_manage failed: {e}")
				return ActionResult(error=f"self_context_manage failed: {e}", include_in_memory=True)

			if not res.ok:
				return ActionResult(error=f"Self-context rejected: {'; '.join(res.errors)}",
				                    include_in_memory=True)
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
				extracted_content="Self-context saved (pending review; applies next session).",
				include_in_memory=True,
			)

	def _register_insights_action(self):
		"""Register the read-only `insights` tool (W7), gated INSIGHTS_TOOL.

		Reports whether the agent's self-authored skills actually get reused — the
		measurement the writable-skills safety brief requires. Tenant-scoped, no writes.
		"""
		try:
			from agents.task.constants import AutonomyConfig
			if not AutonomyConfig.insights_tool():
				return
		except Exception:
			return

		class InsightsAction(BaseModel):
			pass

		@self.registry.action(
			"Show insights about your own learning: how many durable skills you've "
			"authored and how often you reuse them (authored-skill reuse rate).",
			param_model=InsightsAction,
		)
		async def insights(params: InsightsAction, execution_context=None) -> ActionResult:
			user_id = getattr(execution_context, 'user_id', None) or getattr(self, 'user_id', None)
			try:
				from modules.skills.skill_usage import get_skill_usage_store
				summary = get_skill_usage_store().authored_reuse_summary(user_id=user_id)
			except Exception as e:
				self.logger.debug(f"insights failed: {e}")
				return ActionResult(extracted_content="No insights available.", include_in_memory=False)
			rate = round(summary["reuse_rate"] * 100)
			top = ", ".join(f"{t['skill_id']}({t['loads']})" for t in summary["top"][:5]) or "—"
			return ActionResult(
				extracted_content=(
					f"## Skill insights\n"
					f"- authored skills: {summary['authored_total']}\n"
					f"- reused at least once: {summary['authored_reused']} ({rate}%)\n"
					f"- by author: {summary['by_author']}\n"
					f"- most-used: {top}"
				),
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
