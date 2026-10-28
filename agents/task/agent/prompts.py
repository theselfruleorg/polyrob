from datetime import datetime, timezone
from typing import List, Optional

# Native message types
from modules.llm.messages import HumanMessage, SystemMessage

from agents.task.agent.views import ActionResult, AgentBrain, AgentStepInfo
from core.prompt_text import prompt_text as _p
from agents.task.loop_types.browser_views import BrowserState


class SystemPrompt:
	# Defaults for a partially-built instance (tests build one with __new__).
	sub_agent = False
	autonomous = False
	surface = None
	max_failures = None
	_tool_ids_known = False
	tool_ids: list = []

	def __init__(
		self,
		action_description: str,
		max_actions_per_step: int = 10,
		use_native_tools: bool = False,
		model_name: str = "",
		provider: str = "",
		mcp_servers: Optional[dict] = None,
		include_vision: bool = True,
		include_browser_tools: bool = True,  # NEW: Toggle browser tools section
		persona_block: Optional[str] = None,  # S1: chat-mode persona appended to <identity>
		tool_ids: Optional[List[str]] = None,  # Session's loaded tool_ids for config-aware gating
		autonomous: bool = False,  # §3.3: goal/cron/autonomous session -> communication contract
		surface: Optional[dict] = None,  # G6: bound chat surface profile -> <surface> block
		verbosity: Optional[str] = None,  # C2: style.verbosity -> <message-shape> budget
		sub_agent: bool = False,  # A4: a delegated leaf — done(text) is its report to the parent
		max_failures: Optional[int] = None,  # A4: the session's real failure limit (profile)
		owner_seat: bool = False,  # 081 WS-3.1: an owner chat seat always pins <tool-catalog>
	):
		# The tools actually loaded this session. Used to gate config-aware sections
		# (e.g. <anysite>, <browser-tools>, <input-format>, the no-MCP fallback) on the
		# real tool set so the prompt never advertises a tool this session cannot call.
		# None => legacy caller that never passes tool_ids: every section keeps its
		# pre-gating behavior (back-compat); [] => a session that genuinely loaded
		# nothing gets nothing advertised.
		self._tool_ids_known = tool_ids is not None
		self.tool_ids = list(tool_ids or [])
		# S1 (chat consolidation): optional character/personality text injected
		# AFTER the static identity sentence so the prompt-cache stable prefix is
		# preserved. Empty/None => byte-identical to the legacy prompt.
		self.autonomous = bool(autonomous)
		# G6: the bound surface's own capabilities (id / message cap / media /
		# ask), resolved once at session construction from SurfaceCapabilities.
		# None => surface-agnostic (goal/cron/`polyrob run`/raw API) and the
		# prompt is byte-identical to the pre-G6 build.
		self.surface = surface if isinstance(surface, dict) and surface else None
		# C2 (communication contract): the ONE message-length budget, resolved from
		# the owner's `style.verbosity` pref at construction (session-stable, so the
		# prompt stays byte-identical across steps and the cache holds). Unknown or
		# unset falls back to "normal" — the budget must always have a number,
		# because "be concise" with no number is what the model ignored.
		self.verbosity = str(verbosity).strip().lower() if verbosity else "normal"
		self.persona_block = (persona_block or "").strip()
		# A4: a sub-agent's done(text) IS the result its parent reads, and its
		# send_message reaches no person — the opposite of the top-level contract.
		self.sub_agent = bool(sub_agent)
		self.owner_seat = bool(owner_seat)
		self.max_failures = max_failures if isinstance(max_failures, int) and max_failures > 0 else None
		self.action_descriptions = action_description
		self.max_actions_per_step = max_actions_per_step  # config ceiling; the executor cap is lower
		self.use_native_tools = use_native_tools  # Whether using native tool calling
		self.model_name = model_name.lower() if model_name else ""
		self.provider = provider.lower() if provider else ""
		# MCP servers dict: {server_name: [tool_names]} - for dynamic prompt generation
		self.mcp_servers = mcp_servers or {}
		self.include_vision = include_vision  # Whether to include vision instructions
		# T1-06: an explicit include_browser_tools=False always wins; otherwise the
		# session's real tool_ids decide (browser loaded => browser sections render).
		# Legacy callers that pass neither keep the old always-on behavior.
		self.include_browser_tools = include_browser_tools and (
			not self._tool_ids_known or "browser" in self.tool_ids
		)

	def _has_tool(self, tool_id: str) -> bool:
		"""A legacy caller (tool_ids unknown) keeps every mention; otherwise only
		a tool this session actually loaded is named."""
		return not self._tool_ids_known or tool_id in self.tool_ids

	def _get_model_specific_instructions(self) -> str:
		"""A short per-model-family operational note (Gemini/GPT/Kimi families),
		matched on the model name. Anthropic/Claude gets none."""
		parts = []
		from agents.task.constants import MODEL_FAMILY_INSTRUCTIONS
		name = self.model_name or ""
		for needle, instructions in MODEL_FAMILY_INSTRUCTIONS:
			if needle in name:
				parts.append(self._drop_reply_bullet(instructions) if self.sub_agent
				             else instructions)
				break

		return "\n".join(parts)

	@staticmethod
	def _drop_reply_bullet(note: str) -> str:
		"""A4: a sub-agent never replies with send_message — drop the family
		note's reply bullet (and its continuation lines) for it."""
		out, skipping = [], False
		for line in note.splitlines():
			if line.startswith("- "):
				skipping = "send_message(text=" in line
			elif not line.startswith("  "):
				skipping = False
			if not skipping:
				out.append(line)
		return "\n".join(out) + ("\n" if note.endswith("\n") else "")

	def _get_actions_section(self) -> str:
		"""The action list — JSON-fallback mode only. In native mode the schemas
		ship in ``tools[]`` and ``<rules>`` names them, so a second list here only
		repeated them (081 WS-3.2)."""
		if self.use_native_tools:
			return ""
		return _p("actions.json", actions=self.action_descriptions)

	def _get_mcp_section(self) -> str:
		"""Generate dynamic MCP section based on available servers.

		If no MCP servers are configured, returns a minimal section.
		Otherwise, generates examples using actual server/tool names.

		FIX (Jan 2026): LLMs struggle with nested mcp_execute_tool(arguments={...}).
		Now MCP tools are registered as DIRECT actions with flat parameters.
		Example: anysite_api(endpoint='/api/linkedin/user', params={'user': 'foo'})

		Flow-efficiency D4-c (2026-06): the system prompt is built ONCE per session
		and reused every step, so it must stay stable for prompt caching. The former
		step-based "progressive compression after step 3" was dead (step_number was
		never threaded through) and has been removed in favor of one stable section.
		"""
		if not self.mcp_servers:
			# No MCP servers — point at alternatives, but ONLY ones actually loaded
			# this session (T1-11). Legacy callers without tool_ids keep the old text.
			# 093 review: an owner seat is told the one remedy (/mcp add).
			owner_add = ([_p("mcp_tools.owner_add")]
			             if self.owner_seat and not self.sub_agent else [])  # a leaf cannot propose
			if not self._tool_ids_known:
				return "\n".join([_p(f"mcp_tools.{k}") for k in
				                   ("off", "alt_browser", "alt_perplexity", "alt_files")]
				                  + owner_add)
			alternatives = [_p(f"mcp_tools.alt_{tid}") for tid in ("browser", "perplexity", "web_fetch")
			                if tid in self.tool_ids]
			if not alternatives:
				# Nothing to route to — drop the section entirely rather than
				# describing an absence (the <using-your-tools> principle covers it).
				return ""
			return "\n".join([_p("mcp_tools.off"), *alternatives, *owner_add])

		# Build direct action examples for each server
		direct_examples = []
		tools_list = []

		for server_name, tools in self.mcp_servers.items():
			if tools:
				# Show sample tools with their DIRECT action names
				sample_tools = []
				for tool in tools[:5]:
					direct_name = f"{server_name}_{tool}"
					sample_tools.append(direct_name)

				if len(tools) > 5:
					tools_list.append(f"- **{server_name}**: {', '.join(sample_tools)}, ... (+{len(tools)-5} more)")
				else:
					tools_list.append(f"- **{server_name}**: {', '.join(sample_tools)}")

				# Add a concrete example for this server
				for tool in tools:
					tool_lower = tool.lower()
					if 'search' in tool_lower or 'duckduckgo' in tool_lower:
						direct_name = f"{server_name}_{tool}"
						direct_examples.append(f'{direct_name}(query="AI startups", count=10)')
						break
					elif 'linkedin' in tool_lower and 'user' in tool_lower:
						direct_name = f"{server_name}_{tool}"
						direct_examples.append(f'{direct_name}(keywords="founder CEO", count=5)')
						break

		tools_section = "\n".join(tools_list) if tools_list else _p("mcp_tools.no_servers_listed")

		# Build examples section
		if direct_examples:
			examples_text = "\n".join([f"- {ex}" for ex in direct_examples[:3]])
		else:
			# Fallback generic example
			first_server = next(iter(self.mcp_servers.keys()))
			first_tool = self.mcp_servers[first_server][0] if self.mcp_servers[first_server] else "tool"
			examples_text = f"- {first_server}_{first_tool}(query=\"your search\", count=10)"

		_alts = [name for tid, name in (("browser", "browser"), ("perplexity", "perplexity"),
		                                 ("web_fetch", "web_fetch_fetch_url"))
		         if self._has_tool(tid)]
		_alt_line = (_p("mcp_tools.retry_alternatives", alternatives=", ".join(_alts))
		             if _alts else _p("mcp_tools.retry_none"))
		return _p("mcp_tools.on", examples=examples_text, tools=tools_section, retry=_alt_line)

	def _get_subtask_section(self) -> str:
		"""Generate subtask delegation section only if sub-agents are enabled."""
		from agents.task.constants import TimeoutConfig

		# A4: a sub-agent is a leaf — delegate_task is refused for it.
		if self.sub_agent or not TimeoutConfig.get_sub_agents_enabled():
			# Sub-agents disabled - don't mention them at all
			return ""
		
		# The call shapes live in the delegate_task description; this is only the
		# when-to-use trade-off.
		return _p("subtask_delegation")

	def _get_vision_section(self) -> str:
		"""Generate the vision section — CAN-see or CANNOT-see, never silence.

		2026-09-13: ``include_vision=False`` used to render an empty string "to save
		tokens". That left a blind model (prod runs glm-5 via zai-coding, declared
		``supports_vision=False``) with no instruction at all, while
		``modules/llm/adapters.py`` quietly swapped the image for an ``[IMAGE]``
		placeholder — exactly the setup for narrating a picture it never saw. The
		owner's call was honest refusal: say plainly that you cannot see it.
		"""
		if not self.include_vision:
			return _p("vision.off")
		# T1-11: only name the browser action when the browser is actually loaded —
		# the prohibition is meaningless (and advertises a missing tool) without it.
		return _p("vision.on", browser_dont=(_p("vision.browser_dont")
		                                     if self.include_browser_tools else ""))

	def input_format(self) -> str:
		return _p("input_format")

	def _get_memory_system_content(self) -> str:
		"""The <memory-system> section — rendered by ``prompt_memory`` (E-A11 moved
		it out so the backend gate did not grow this file)."""
		from agents.task.agent.prompt_memory import memory_system_content
		return memory_system_content()

	def _get_communication_content(self) -> str:
		"""Get communication section content.

		T1-15: states the REAL turn-end contract — the runtime ends a turn after
		CONVERSATIONAL_EXIT_AFTER_REPLIES consecutive reply-only steps (conversational
		exit), so the old "a non-blocking send never ends your turn" claim was false.
		The threshold is session-stable, so deriving it at build time is cache-safe.
		"""
		if self.sub_agent:
			return _p("communication.leaf")
		try:
			from agents.task.agent.core.conversational_exit import (
				CONVERSATIONAL_EXIT_AFTER_REPLIES as _exit_after,
			)
		except Exception:
			_exit_after = 2
		return _p("communication.main", owner_moves=self._owner_moves_line(),
		          exit_after=_exit_after)

	def _owner_facing(self) -> bool:
		"""The session speaks to the OWNER: not a room, not a correspondent, not a leaf."""
		if self.sub_agent:
			return False
		s = self.surface or {}
		if s.get("correspondent"):
			return False
		return str(s.get("chat_type") or "dm") == "dm"

	def _owner_moves_line(self) -> str:
		"""B1: present_choice, for a live owner turn only."""
		if self.autonomous or not self._owner_facing():
			return ""
		# 093 C3: a missing capability is read from the harness, then answered once.
		return _p("communication.owner_moves") + "\n" + _p("harness")

	def _get_rules_content(self) -> str:
		"""Get critical rules section content.

		T1-04: state the REAL runtime contract, not a false threat. The old text
		("VIOLATION = REJECTION. After 3 failures, session halts") threatened rejection
		for the tool-free planning turn the runtime deliberately allows
		(ALLOWED_REASONING_TURNS) and cited the wrong halt threshold (the real one is
		DEFAULT_MAX_FAILURES). Both constants are session-stable, so deriving them at
		build time keeps the prompt cache-stable.
		"""
		try:
			from agents.task.constants import DEFAULT_MAX_FAILURES, ALLOWED_REASONING_TURNS
		except Exception:
			DEFAULT_MAX_FAILURES, ALLOWED_REASONING_TURNS = 5, 1
		if getattr(self, "max_failures", None):
			DEFAULT_MAX_FAILURES = self.max_failures
		if ALLOWED_REASONING_TURNS and ALLOWED_REASONING_TURNS > 0:
			plan = _p("rules.plan_turns", n=ALLOWED_REASONING_TURNS,
			          turns="turn" if ALLOWED_REASONING_TURNS == 1 else "turns")
		else:
			plan = _p("rules.plan_always")
		return _p("rules.main", plan=plan, max_failures=DEFAULT_MAX_FAILURES)

	def _get_browser_content(self) -> str:
		"""Get web/browser tools section content (tier routing).

		F1: the action names are the registered ones (browser_click_element,
		browser_input_text; there is no screenshot action). F11: the READ /
		SEARCH routes name only tools this session loaded.
		"""
		lines = [_p("web_access.head")]
		if self._has_tool("web_fetch"):
			lines.append(_p("web_access.browser_read"))
		search = [_p(f"web_access.browser_search_{tid}") for tid in ("perplexity", "anysite")
		          if self._has_tool(tid)]
		if search:
			lines.append(_p("web_access.browser_search", sources=" or ".join(search)))
		lines.append(_p("web_access.browser_interact"))
		tips = [_p("web_access.tip_fetch"), _p("web_access.tip_shell")
		        ] if self._has_tool("web_fetch") else []
		tips.append(_p("web_access.tip_browser"))
		return ("\n".join(lines) + _p("web_access.browser_actions")
		        + "\n".join(f"{i}. {t}" for i, t in enumerate(tips, 1)))

	def _get_web_access_content(self) -> str:
		"""Tier-routing guidance for sessions WITHOUT the browser tool (T1-06/11).

		The routing lines used to live only inside <browser-tools>, so a session
		with web_fetch/perplexity but no browser lost the fetch_url teaching
		entirely. Render only the tiers this session can actually reach, and be
		honest that interactive browsing is not available.
		"""
		lines = [_p("web_access.head")]
		for tid, key in (("web_fetch", "read"), ("perplexity", "search"), ("anysite", "anysite")):
			if tid in self.tool_ids:
				lines.append(_p(f"web_access.{key}"))
		lines.append(_p("web_access.interact_missing"))
		return "\n".join(lines)

	def _get_filesystem_content(self) -> str:
		"""Get filesystem section content.

		F11: the large-content numbers and the pointer format are the offload
		path's own (``result_offload.py`` + ``RobustParseConfig``), read at build
		time (session-stable, cache-safe).
		"""
		try:
			from agents.task.robust_parse_config import RobustParseConfig as _R
			per_result, per_turn = int(_R.MAX_EXTRACTED_CONTENT_SIZE), int(_R.MAX_EXTRACTED_CONTENT_TURN_SIZE)
		except Exception:
			per_result, per_turn = 100000, 200000
		try:
			from agents.task.agent.core.result_budget import tool_result_max_tokens
			wire_chars = int(tool_result_max_tokens()) * 4
		except Exception:
			wire_chars = 24000
		_wire = _p("filesystem.wire", wire_chars=f"{wire_chars:,}") if wire_chars > 0 else ""
		return _p("filesystem.main", per_result=f"{per_result:,}", per_turn=f"{per_turn:,}",
		          wire=_wire)

	def _status_tool_examples(self) -> str:
		"""F11: name goal_list / recent_activity only when this session has them."""
		names = []
		if self._has_tool("goal"):
			names.append("goal_list")
		if "`recent_activity`" in self._get_memory_system_content():
			names.append("recent_activity")
		return (_p("using_your_tools.status_named", names=", ".join(names)) if names
		        else _p("using_your_tools.status_generic"))

	def _get_tools_section(self) -> str:
		"""Get consolidated tool capabilities section with XML tags.

		Config-aware sections (<anysite>, <browser-tools>/<web-access>, the no-MCP
		fallback) gate on the session's real tool_ids (T1-06/11) so the prompt never
		advertises a tool the session cannot call.
		"""
		sections = []

		# Config-awareness principle (static, always present, cache-stable): the agent
		# must reason from the tools it ACTUALLY has this session — closes the
		# self-model drift that produced "the goal DB is missing" (session lacked the
		# goal tool → it read the filesystem instead of calling the tool) and "I can't
		# do voice" (denying a configured capability). No per-session interpolation.
		sections.append("<using-your-tools>\n"
		                + _p("using_your_tools", status_tools=self._status_tool_examples())
		                + "\n</using-your-tools>")

		# MCP Tools — one stable section per session (see _get_mcp_section docstring)
		mcp = self._get_mcp_section()
		if mcp:
			sections.append(f"<mcp-tools>\n{mcp}\n</mcp-tools>")

		# Browser Tools — conditional on the session actually having the browser
		# (T1-06). Without it, a session that still has web tools gets the honest
		# <web-access> tier-routing instead so fetch_url/perplexity stay taught.
		if self.include_browser_tools:
			sections.append(f"<browser-tools>\n{self._get_browser_content()}\n</browser-tools>")
		elif self._tool_ids_known and ({"web_fetch", "perplexity"} & set(self.tool_ids)):
			sections.append(f"<web-access>\n{self._get_web_access_content()}\n</web-access>")

		# Filesystem
		sections.append(f"<filesystem>\n{self._get_filesystem_content()}\n</filesystem>")

		return "\n\n".join(sections)

	def _get_response_format_content(self) -> str:
		"""Get response format section content."""
		if self.use_native_tools:
			return _p("response_format.native", n=self._effective_actions_per_step(),
			          todo_lines=self._todo_lines())
		return _p("response_format.json")

	def _todo_lines(self) -> str:
		if not self._has_tool("task"):
			return ""
		return _p("response_format.todos")

	def _get_agency_content(self) -> str:
		"""T1-05: tell the agent to act with judgment INSIDE its rails.

		Nothing else in the stack tells the agent to be autonomous/resourceful — the
		default identity is a passive "specialist" and every other instruction is
		prohibition-shaped, which reads as "wait to be told." This static, cache-stable
		block gives it a charter to decide and act (without weakening any security rail —
		the tool/permission system is still the boundary).
		"""
		return _p("agency.main", standing_goals=(_p("agency.standing_goals")
		                                         if self._owner_facing() else ""))

	def _get_surface_content(self) -> str:
		"""G6: the shape rule for the surface this session actually speaks into.

		Derived once per session from ``SurfaceCapabilities`` (id, message cap,
		media, ask), so the text is stable across the session's steps and the
		prompt cache is unaffected. Only rendered when a chat surface is bound.
		"""
		s = self.surface or {}
		surface_id = str(s.get("surface_id") or "chat")
		cap = int(s.get("max_message_bytes") or 0)
		lines = [_p("surface.speaking", surface_id=surface_id)]
		if cap:
			lines.append(_p("surface.cap", cap=cap))
		if s.get("media_out"):
			# G11: name the MECHANISM, not just the preference — "attach it"
			# is not actionable unless the model knows which verb attaches.
			# Gated on the action actually being registered, so this never
			# advertises a call this session cannot make (the T8 rule).
			how = ""
			try:
				from core.config_policy import message_tool_enabled
				if message_tool_enabled():
					how = _p("surface.media_how")
			except Exception:
				how = ""
			lines.append(_p("surface.media", how=how))
			if how:
				# 2026-09-13: naming the verb was not enough — artifacts were
				# "delivered" as a workspace path in prose, which the reader
				# cannot open. Send each file explicitly. (`done()` never
				# reaches a chat surface: only `send_message` / `message` speak.)
				lines.append(_p("surface.media_attach"))
		else:
			lines.append(_p("surface.no_media"))
		if not s.get("supports_interactive_ask"):
			lines.append(_p("surface.no_ask"))
		# 044 T15: the ROOM paragraph — everything written there is public, a
		# <group-context> line is context, and owner questions are not answered.
		if str(s.get("chat_type") or "dm") != "dm":
			lines.append(_p("surface.room", surface_id=surface_id))
			# `done` has no delivery path; in a room a done-only turn says nothing.
			lines.append(_p("surface.room_speak"))
			# 044 T17: the room's STANDING instructions (`chat.instructions`),
			# owner-authored and threat-scanned at write (chat_policy.set).
			_room_rules = str(s.get("chat_instructions") or "").strip()
			if _room_rules:
				lines.append(_p("surface.room_rules", rules=_room_rules))
			# 046: what this room sells, rendered by core from the room's own
			# prices (`room_actions.describe_for_model`) — a description, not a verb.
			_room_paid = str(s.get("chat_paid_actions") or "").strip()
			if _room_paid:
				lines.append(_room_paid)
		elif s.get("correspondent"):
			# C2: a DM driven by a third party is NOT the owner's chat — no owner
			# framing, no owner verbs (they are refused for this session anyway).
			lines.append(_p("surface.correspondent", surface_id=surface_id))
		else:
			lines.extend(self._owner_action_shape_lines(surface_id))
		# C2: the shape rules themselves live in <message-shape>, which every session
		# gets. This block carries ONLY what is specific to the bound surface.
		return "\n".join(lines)

	def _owner_action_shape_lines(self, surface_id: str) -> list:
		"""How to ASK the owner to do something, on a chat surface (2026-09-15).

		The owner reads this on a phone. A chat client auto-links ONE `/word`
		token and sends the whole token on tap; it does not link a trailing
		argument, and it cannot run a shell at all. So `polyrob owner pending`
		and `/approve <id>` are both dead ends there — the first has nowhere to
		be typed, the second leaves the owner copying an id by hand off a phone.
		Both went out live, repeatedly, because nothing in this prompt ever said
		what an ACTIONABLE request looks like on the seat the agent speaks into.

		Per-session stable (the verb vocabulary is a module constant), so the
		system prompt stays byte-stable across steps and the cache holds.
		"""
		try:
			from core.owner_remedy import chat_verbs
			verbs = " ".join(sorted(chat_verbs()))
		except Exception:
			verbs = ""
		lines = [_p("surface.owner", surface_id=surface_id)]
		if verbs:
			lines.append(_p("surface.owner_verbs", verbs=verbs))
		return lines

	#: C2: the ONE line budget per `style.verbosity` value. Mirrors
	#: ``core.prefs._VERBOSITY_GUIDANCE`` — the pref is the SSOT for the VALUE, this
	#: is how the value is spoken to the model. A bare adjective ("be terse") has no
	#: operational content; a number does.
	_VERBOSITY_BUDGET = {k: _p(f"message_shape.budget_{k}")
	                     for k in ("terse", "normal", "detailed")}

	def _get_message_shape_content(self) -> str:
		"""C2: how a message to the user is shaped — on EVERY session.

		Before this block, every shape rule was conditional: the ``<surface>`` bullets
		needed a bound chat surface, the ``<communication-contract>`` bullets needed an
		autonomous session, and the prefs style line needed a pref to be set. A plain
		interactive session therefore had NO length rule at all (finding F9) and the
		three sources disagreed on the number when more than one did fire.

		Static per session (the budget is resolved once at construction), so the
		system prompt stays byte-stable across steps and the prompt cache holds.
		"""
		budget = self._VERBOSITY_BUDGET.get(
			self.verbosity, self._VERBOSITY_BUDGET["normal"])
		return _p("message_shape.main", budget=budget)

	def _get_communication_contract_content(self) -> str:
		"""§3.3 (intelligence-stack finalization): the agent OWNS keeping its user
		informed in autonomous sessions. Static, cache-stable text — behavior is
		shaped by contract + post-run verification, not per-event framework rails."""
		# Owner rule (2026-09-29): the report a goal/cron run delivers is its
		# send_message text (cron `deliver` sends the LAST one); done(text) is the
		# run record, shown to the owner only at style.verbosity=detailed.
		goal_line = _p("communication_contract.goal_board"
		               if (not self._tool_ids_known or "goal" in self.tool_ids)
		               else "communication_contract.no_goal_board")
		return _p("communication_contract.main", goal_line=goal_line)

	def _get_owner_instruction_routing(self) -> str:
		"""035 P1-9 — where an owner instruction has to be RECORDED.

		The 2026-09-08 failure was not a refusal and not a plumbing fault: the agent
		understood "stop posting to the group room", agreed, and wrote it to the one
		lane that cannot enforce anything — while the lane that applies immediately and
		is enforced at 56 read sites (typed preferences) went unused for 30 days. No
		prompt named any of the write lanes, and two tool descriptions both advertised
		"preferences", so the choice was a coin flip. This is the missing routing.

		Static text => prompt-cache-stable.
		"""
		try:
			from core.config_policy import AutonomyConfig, prefs_tool_enabled
			prefs_on = prefs_tool_enabled()
			owner_doc_on = AutonomyConfig.owner_doc_writable()
			self_ctx_on = AutonomyConfig.self_context_writable()
		except Exception:
			prefs_on = owner_doc_on = self_ctx_on = False
		# F7: each bullet only when ITS tool's flag is on — never name a lane
		# this session cannot write.
		parts = ["head"] + [k for k, on in (("prefs", prefs_on), ("owner_doc", owner_doc_on),
		                                    ("self_doc", self_ctx_on)) if on] + ["tail"]
		return "\n".join(_p(f"owner_instructions.{k}") for k in parts)

	def _get_security_content(self) -> str:
		"""UP-06: teach the model that <untrusted_tool_result> content is DATA.

		Static string (no per-step interpolation) so the system prompt stays
		byte-stable across steps and prompt-cache-friendly.
		"""
		return _p("security")

	def _get_source_precedence_content(self) -> str:
		"""Tell the model which instruction source is authoritative (anti-stale-drift).

		Static (no per-step interpolation) so the system prompt stays cache-stable.
		"""
		# F11: the <tool-catalog> block is pinned only under tool disclosure
		# (agents.task.session_class.tool_disclosure_enabled); without it the
		# model is told about <tool-availability> instead. F8 (063 WS-4): the
		# pinned item says a later <tool-catalog-update> wins.
		# 060 WS-4 (owner decision Q1, 2026-09-23): the one instruction-authority
		# order lives in the block text.
		catalog_item = _p("source_precedence.catalog_pinned" if self._catalog_pinned()
		                  else "source_precedence.catalog_tools")
		return _p("source_precedence.main", live_state=self._live_state_line(),
		          catalog_item=catalog_item)

	def _live_state_line(self) -> str:
		"""Review 2026-10-15 rec. 2: switch state is live state, not prose. Only
		where <operations> rides the turn (owner seats and autonomous runs)."""
		if not self._owner_facing():
			return ""
		return _p("source_precedence.live_state")

	def _catalog_pinned(self) -> bool:
		"""Whether this session pins <tool-catalog> — the rule of
		``tool_disclosure_enabled`` (progressive disclosure, an owner chat seat, or
		the autonomous key for an autonomous session). Fail-open to True."""
		if getattr(self, "owner_seat", False):
			return True
		try:
			from core.config_policy import tool_progressive_disclosure
			if tool_progressive_disclosure():
				return True
			from core.config_policy.capability_toggles import autonomous_tool_disclosure
			return bool(autonomous_tool_disclosure() and getattr(self, "autonomous", False))
		except Exception:
			return True

	def _effective_actions_per_step(self) -> int:
		"""The number of tool calls ONE step will actually execute: the config ceiling
		capped by the executor's hard limit, so the prompt never promises a batch the
		executor will truncate (2026-09-19: "1-10" promised, 5 run, the 6th vanished)."""
		try:
			from agents.task.agent.core.step_execution import MAX_TOOL_CALLS_PER_STEP
			return max(1, min(int(self.max_actions_per_step), int(MAX_TOOL_CALLS_PER_STEP)))
		except Exception:
			return max(1, int(self.max_actions_per_step))

	def get_system_message(self) -> SystemMessage:
		"""
		Get the system prompt with explicit XML-tagged sections.

		Returns:
		    SystemMessage with XML-structured prompt
		"""
		# Get model-specific instructions if needed (e.g., for Grok)
		model_specific_instructions = self._get_model_specific_instructions()

		# S1: chat-mode persona, appended after the static identity sentence.
		# Empty => "" => byte-identical to the legacy <identity> block.
		persona_section = f"\n{self.persona_block}" if self.persona_block else ""

		# Get conditional sections
		subtask_section = self._get_subtask_section()
		vision_section = self._get_vision_section()

		# Build optional sections
		optional_sections = ""
		if subtask_section:
			optional_sections += f"\n<subtask-delegation>\n{subtask_section}\n</subtask-delegation>\n"
		if vision_section:
			optional_sections += f"\n<vision-capabilities>\n{vision_section}\n</vision-capabilities>\n"
		# UP-06: injection-defense notice (gated; static => prompt-cache-stable)
		try:
			from agents.task.constants import UNTRUSTED_TOOL_RESULT_WRAP
		except Exception:
			UNTRUSTED_TOOL_RESULT_WRAP = False
		if UNTRUSTED_TOOL_RESULT_WRAP:
			optional_sections += f"\n<security>\n{self._get_security_content()}\n</security>\n"
		# 035 P1-9: route an owner instruction to the lane that can enforce it.
		# Per-session stable (flag-derived), so prompt caching is unaffected.
		try:
			from core.config_policy import AutonomyConfig, prefs_tool_enabled
			_routing_on = (AutonomyConfig.owner_doc_writable()
			               or AutonomyConfig.self_context_writable()
			               or prefs_tool_enabled())
		except Exception:
			_routing_on = False
		# B-14: only where the owner speaks — not a room, a correspondent, a leaf
		# or an autonomous run (there a decision is an owner_ask).
		if _routing_on and self._owner_facing() and not self.autonomous:
			optional_sections += (f"\n<owner-instructions>\n"
			                      f"{self._get_owner_instruction_routing()}\n"
			                      f"</owner-instructions>\n")
		# T8 (013 owner transparency directive): disclose gated/missing tools + remedy.
		# Skip when tool_ids is unknown (legacy caller) — never claim an absence we
		# can't verify. Per-session stable (varies only with the session's tool_ids,
		# same as the existing config-aware sections) so prompt caching is unaffected.
		try:
			from agents.task.agent.core.tool_availability import build_tool_availability_note
			if self._tool_ids_known:
				note = build_tool_availability_note(
					set(self.tool_ids), can_create_goals=self._owner_facing(),
					catalog_pinned=self._catalog_pinned())
				if note:
					optional_sections += f"\n{note}\n"
		except Exception:
			pass
		# G6: the bound surface's shape rule. Per-session stable (resolved once at
		# construction), so prompt caching is unaffected; absent when no chat
		# surface is bound -> byte-identical to the pre-G6 prompt.
		if self.surface:
			optional_sections += (f"\n<surface>\n"
			                      f"{self._get_surface_content()}\n"
			                      f"</surface>\n")
		# C2: the message-shape contract, on EVERY session. Unconditional by design —
		# it replaces three conditional, mutually-disagreeing sources (the <surface>
		# bullets, the <communication-contract> bullets, and the prefs style line),
		# so an unbound interactive session is no longer the one seat with no rule.
		# Session-stable (the budget resolves once at construction) => cache-safe.
		# A4: a sub-agent writes to its parent, never to a person.
		if not self.sub_agent:
			optional_sections += (f"\n<message-shape>\n"
			                      f"{self._get_message_shape_content()}\n"
			                      f"</message-shape>\n")
		# §3.3: autonomous sessions carry the communication contract (static text,
		# gated on a per-session flag -> byte-stable across the session's steps).
		if self.autonomous:
			optional_sections += (f"\n<communication-contract>\n"
			                      f"{self._get_communication_contract_content()}\n"
			                      f"</communication-contract>\n")
		try:
			from core.config_policy import _bool_env
			_precedence_on = _bool_env("SOURCE_PRECEDENCE_PROMPT", True)
		except Exception:
			_precedence_on = True
		if _precedence_on:
			optional_sections += f"\n<source-precedence>\n{self._get_source_precedence_content()}\n</source-precedence>\n"

		# T1-06: <input-format> describes browser state (URL / tabs / interactive
		# elements) — inject it only when the session can actually drive a browser.
		if self.include_browser_tools:
			input_format_section = f"\n<input-format>\n{self.input_format()}\n</input-format>\n"
		else:
			input_format_section = ""

		_actions = self._get_actions_section()
		actions_section = f"\n<available-actions>\n{_actions}\n</available-actions>\n" if _actions else ""
		AGENT_PROMPT = f"""<system-prompt>

<identity>
{_p("identity")}
{model_specific_instructions}{persona_section}
</identity>

<response-format>
{self._get_response_format_content()}
</response-format>

<memory-system>
{self._get_memory_system_content()}
</memory-system>

<tool-capabilities>
{self._get_tools_section()}
</tool-capabilities>

{{SKILLS_PLACEHOLDER}}

<communication>
{self._get_communication_content()}
</communication>

<agency>
{self._get_agency_content()}
</agency>
{optional_sections}
<rules>
{self._get_rules_content()}
</rules>
{input_format_section}{actions_section}
</system-prompt>"""
		return SystemMessage(content=AGENT_PROMPT)


_BLANK_PAGE_URLS = frozenset({"", "about:blank", "chrome://newtab", "chrome://new-tab-page"})


def is_blank_page_url(url) -> bool:
	"""True when the browser sits on a page nobody navigated to.

	2026-09-19: a fresh session context is `about:blank`, and
	`get_state(capture_screenshot=use_vision)` shoots it on EVERY step, so a
	money-rail run that never touches the browser still shipped one image per
	call through the metered seat's vision path (prod: `Sending 1 image(s)`
	from step 1 on every EXIT/SAFETY/WATCHER/SCOUT run). A blank page has no
	visual information; the render site skips the image for it.
	"""
	if not url:
		return True
	u = str(url).strip().lower().rstrip("/")
	return u in _BLANK_PAGE_URLS


class AgentMessagePrompt:
	def __init__(
		self,
		state: BrowserState,
		result: Optional[List[ActionResult]] = None,
		include_attributes: Optional[list[str]] = None,
		max_error_length: int = 400,
		step_info: Optional[AgentStepInfo] = None,
		previous_brain: Optional['AgentBrain'] = None,
		include_browser_state: bool = True,  # NEW: Toggle browser context injection
		results_in_tool_messages: bool = False,  # F14: results already sent as ToolMessages
	):
		self.state = state
		self.result = result
		self.max_error_length = max_error_length
		self.step_info = step_info
		self.previous_brain = previous_brain  # Previous step's brain state
		self.include_browser_state = include_browser_state
		# F14 (063 WS-3): True when the step that produced ``result`` already
		# committed those bytes to history as ToolMessages (the native tool-calling
		# path every shipped provider uses). The state message then carries only the
		# step counter, the clock, the brain-state continuity and the browser DOM.
		# False = the legacy no-native-tools path, where the results never became
		# ToolMessages and this render is their ONLY appearance.
		self.results_in_tool_messages = results_in_tool_messages
		self.include_attributes = include_attributes or [
			'title',
			'type',
			'name',
			'role',
			'tabindex',
			'aria-label',
			'placeholder',
			'value',
			'alt',
			'aria-expanded',
		]

	def _has_meaningful_browser_state(self) -> bool:
		"""Check if browser state has meaningful content (not placeholder).
		
		Returns False if:
		- URL is empty or None
		- Title is "No Browser" (placeholder)
		- No interactive elements
		
		This prevents injecting useless browser context for non-browser tasks.
		"""
		# Empty URL = no real browser
		if not self.state.url or self.state.url == "":
			return False
		# Placeholder title = no real browser
		if self.state.title == "No Browser":
			return False
		# Check for actual interactive elements
		try:
			elements_text = self.state.element_tree.clickable_elements_to_string(
				include_attributes=self.include_attributes
			)
			if elements_text == "" or elements_text == "empty page":
				return False
		except Exception:
			return False
		return True

	def get_user_message(self, use_vision: bool = True) -> HumanMessage:
		"""Get the user message for the agent with CONDITIONAL browser state.
		
		FIX (Jan 2026): Browser context is now conditional to prevent bleeding
		into non-browser tasks. Saves ~330 tokens/step for file/code tasks.
		"""
		# Build step info (always included)
		if self.step_info:
			step_info_description = f'Current step: {self.step_info.step_number + 1}'  # 083 WS-5: no denominator
		else:
			step_info_description = ''
		time_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
		step_info_description += f' | Current date and time: {time_str}'

		# Build memory section from previous brain state
		# NOTE: This is kept for immediate context, H-MEM provides longer-term memory
		memory_section = ""
		if self.previous_brain:
			memory_section = "[MEMORY FROM PREVIOUS STEP]\n"

			if self.previous_brain.memory:
				memory_section += f"Working Memory: {self.previous_brain.memory}\n"

			if self.previous_brain.next_goal:
				memory_section += f"Previous Goal: {self.previous_brain.next_goal}\n"

			if self.previous_brain.evaluation_previous_goal:
				memory_section += f"Previous Result: {self.previous_brain.evaluation_previous_goal}\n"

			memory_section += "\n"

		# CONDITIONAL: Only include browser state if it has REAL content
		# This fixes the "browser context bleeding" issue - saves ~330 tokens/step
		#
		# The check is: Does the current state have meaningful browser content?
		# - Real URL (not empty, not "about:blank")
		# - Real title (not "No Browser" placeholder)
		# - Real interactive elements (not "empty page")
		#
		# The include_browser_state hint can force skip the check entirely if we KNOW
		# browser wasn't used (optimization), but meaningful state always wins.
		should_include_browser = self._has_meaningful_browser_state() if self.include_browser_state else False

		if should_include_browser:
			# Full browser state format (for browser-based tasks)
			elements_text = self.state.element_tree.clickable_elements_to_string(include_attributes=self.include_attributes)

			has_content_above = (self.state.pixels_above or 0) > 0
			has_content_below = (self.state.pixels_below or 0) > 0

			if elements_text != '':
				if has_content_above:
					elements_text = (
						f'... {self.state.pixels_above} pixels above - scroll or extract content to see more ...\n{elements_text}'
					)
				else:
					elements_text = f'[Start of page]\n{elements_text}'
				if has_content_below:
					elements_text = (
						f'{elements_text}\n... {self.state.pixels_below} pixels below - scroll or extract content to see more ...'
					)
				else:
					elements_text = f'{elements_text}\n[End of page]'
			else:
				elements_text = 'empty page'

			# S5 (2026-09-14): the page's own text is attacker-authorable and was
			# the ONE untrusted surface that never passed through a tool result,
			# so UP-06 never saw it. Frame it as DATA here (source="browser").
			# Gated on UNTRUSTED_TOOL_RESULT_WRAP; 'empty page' is below the
			# min-chars threshold and passes through untouched.
			from agents.task.agent.core.untrusted_render import maybe_wrap_browser_dom
			elements_text = maybe_wrap_browser_dom(elements_text)

			state_description = f"""{memory_section}[CURRENT STATE]
Current url: {self.state.url}
Available tabs:
{self.state.tabs}
Interactive elements from current page:
{elements_text}
{step_info_description}
"""
		else:
			# Minimal state format (for non-browser tasks like file/code operations)
			# Saves ~330 tokens by not including empty URL, tabs, elements
			state_description = f"""{memory_section}[CURRENT STATE]
{step_info_description}
"""

		if self.result and not self.results_in_tool_messages:
			# F14 (063 WS-3): this whole block is the LEGACY no-native-tools render.
			# On the native path the same bytes are already in history as ToolMessages
			# (result_processing._add_tool_messages), so printing them again doubled the
			# uncached suffix of every tool step — the second copy is the one that
			# escaped the cap in the 2026-09-20 incident. Errors are paired into
			# ToolMessages too, so they are not re-rendered either.
			# S5 (2026-09-14): UP-06 framed these bytes for the ToolMessage, then this
			# render printed the SAME bytes raw — so injected page/MCP content arrived
			# as instructions one step later. Wrap AFTER truncation (a frame cut in half
			# is an open tag the rest of the prompt falls into); the stamp comes from
			# the controller resolution result_processing already did.
			from agents.task.agent.core.untrusted_render import maybe_wrap_result_content
			for i, result in enumerate(self.result):
				if result.extracted_content:
					# PHASE 2 FIX (Nov 4, 2025): Apply separate limit for successful content
					# Use config limit, but don't truncate unless really large
					from agents.task.robust_parse_config import RobustParseConfig
					if len(result.extracted_content) > RobustParseConfig.MAX_SUCCESS_LENGTH:
						rendered = result.extracted_content[:RobustParseConfig.MAX_SUCCESS_LENGTH] + f"\n[...truncated {len(result.extracted_content) - RobustParseConfig.MAX_SUCCESS_LENGTH:,} chars]"
					else:
						rendered = result.extracted_content
					# 057 WS-B (prod 2026-09-20 14:04Z): the ToolMessage is capped at
					# TOOL_RESULT_MAX_TOKENS but this render printed the SAME bytes
					# again under the 100k-CHAR limit above — five anysite JSON answers
					# in one step put ~110k uncached tokens into the next call (210k in,
					# a 221 s timeout) with the cap "armed". Same cap, same honest tail,
					# here too. Cap off (0) ⇒ the legacy limit alone, byte-identical.
					from agents.task.agent.core.result_budget import truncate_tool_result
					rendered = truncate_tool_result(rendered)
					rendered = maybe_wrap_result_content(result, rendered)
					state_description += f'\nAction result {i + 1}/{len(self.result)}: {rendered}'
				if result.error:
					# PHASE 2 FIX (Nov 4, 2025): Use config-based error truncation (increased to 2K)
					# Errors are shorter - we don't need full stack traces, just the key info
					from agents.task.robust_parse_config import RobustParseConfig
					if len(result.error) > RobustParseConfig.MAX_ERROR_LENGTH:
						rendered_error = '...' + result.error[-RobustParseConfig.MAX_ERROR_LENGTH:]
					else:
						rendered_error = result.error
					rendered_error = maybe_wrap_result_content(result, rendered_error)
					state_description += f'\nAction error {i + 1}/{len(self.result)}: {rendered_error}'

		if self.state.screenshot and use_vision == True and not is_blank_page_url(self.state.url):
			# Format message for vision model (a blank/never-navigated page is
			# rendered text-only — see is_blank_page_url)
			return HumanMessage(
				content=[
					{'type': 'text', 'text': state_description},
					{
						'type': 'image_url',
						'image_url': {'url': f'data:image/png;base64,{self.state.screenshot}'},
					},
				]
			)

		return HumanMessage(content=state_description)


# UnifiedSystemPrompt removed - deprecated in favor of single SystemPrompt
# Planning is now integrated into the main agent via todo system

def resolve_system_prompt(
    prompt_type: str = "system",
    prompt_source: str = "builtin", 
    prompt_params: dict = None,
    task: str = "",
    **kwargs
) -> SystemMessage:
    """Resolve prompt configuration into a SystemMessage.
    
    Args:
        prompt_type: Type of prompt (system, planner, custom)
        prompt_source: Source of prompt (builtin, inline). Any other value
            resolves to the builtin system prompt.
        prompt_params: Parameters to pass to prompt class
        task: Task description
        **kwargs: Additional parameters
        
    Returns:
        SystemMessage ready for use
    """
    # SystemMessage imported at module level from modules.llm.messages
    import logging
    logger = logging.getLogger(__name__)
    
    # Fix: Ensure prompt_params is always a dict, not a list or other type
    if prompt_params is None:
        prompt_params = {}
    elif isinstance(prompt_params, list):
        logger.warning(f"prompt_params was a list: {prompt_params}, converting to empty dict")
        prompt_params = {}
    elif not isinstance(prompt_params, dict):
        logger.warning(f"prompt_params was type {type(prompt_params)}: {prompt_params}, converting to empty dict")
        prompt_params = {}
    
    # Handle builtin prompts
    if prompt_source == "builtin":
        if prompt_type == "system":
            prompt_obj = SystemPrompt(**prompt_params, **kwargs)
            return prompt_obj.get_system_message()
        elif prompt_type == "planner":
            # Planner prompt is deprecated - use standard system prompt.
            # (T1-16: the old branch passed task=, which SystemPrompt never accepted —
            # it would have raised TypeError if ever hit.)
            prompt_obj = SystemPrompt(**prompt_params, **kwargs)
            return prompt_obj.get_system_message()
        else:
            # Default to system prompt
            prompt_obj = SystemPrompt(**prompt_params, **kwargs)
            return prompt_obj.get_system_message()
    
    # Handle inline prompt
    elif prompt_source == "inline":
        # Expect prompt_params to contain the actual prompt text
        prompt_text = prompt_params.get("text", "")
        if not prompt_text:
            # Fall back to builtin if no text provided
            prompt_obj = SystemPrompt(**kwargs)
            return prompt_obj.get_system_message()
        return SystemMessage(content=prompt_text)
    
    # Default fallback
    else:
        # FIX: SystemPrompt expects action_description, not task
        prompt_obj = SystemPrompt(**prompt_params, **kwargs)
        return prompt_obj.get_system_message()
