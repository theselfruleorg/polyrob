import os
from typing import Callable, Dict, Optional, Type, Any, List
from dataclasses import dataclass
from threading import Lock

from pydantic import BaseModel

from agents.task.loop_types.registry_views import ActionModel
from agents.task.loop_types.controller_types import ActionResult
from tools.controller.hooks import HookPipeline
from core.security.taints import correspondent_probe as _correspondent_probe
from tools.controller.mcp_registrar import MCPActionRegistrar
from agents.task.loop_types.execution_context import ActionExecutionContext
from tools.controller.views import DoneAction, SendMessageAction
from agents.task.utils import time_execution_async, time_execution_sync

# Import logging
from agents.task.logging_config import get_task_logger

# Native types
from modules.llm.adapters import BaseChatModel
from modules.llm.messages import AIMessage

# UP-11 god-file split: shared helpers live in _helpers (re-exported here so the
# established `from tools.controller.service import ToolInfo/make_denylist_hook/
# build_load_skill_result` call sites keep working), and the four focused mixins
# compose Controller's behaviour. Imported AFTER the helper re-export to keep the
# import graph acyclic (the mixins import from _helpers, never from service).
from tools.controller._helpers import (
	observe,
	ToolInfo,
	make_denylist_hook,
	build_load_skill_result,
)
from tools.controller.action_registration import ActionRegistrationMixin
from tools.controller.tool_management import ToolManagementMixin
from tools.controller.execution import ExecutionMixin
from tools.controller.introspection import IntrospectionMixin
from tools.controller.tool_call_bridge import ToolCallBridgeMixin


class Controller(ExecutionMixin, ToolManagementMixin, IntrospectionMixin, ToolCallBridgeMixin, ActionRegistrationMixin):
	def __init__(
		self,
		exclude_actions: Optional[List[str]] = None,
		output_model: Optional[Type[BaseModel]] = None,
		tools: Optional[Dict[str, Any]] = None,
		tool_ids: Optional[List[str]] = None,
		# session_id, user_id, workspace_dir removed - get from orchestrator (single source of truth)
		available_file_paths: Optional[List[str]] = None,
		container=None,  # DependencyContainer for loading tools
		orchestrator=None  # SessionOrchestrator for todo actions and session state
		# REMOVED: todo_manager parameter - now handled by TaskTool
	):
		"""Initialize Controller with configuration."""

		# Validate orchestrator is provided (single source of truth for session state)
		if not orchestrator:
			raise ValueError("orchestrator is required for Controller")

		# Basic instance variables
		self.exclude_actions = exclude_actions or []
		self.output_model = output_model
		self.orchestrator = orchestrator  # Store orchestrator reference
		# REMOVED: self.todo_manager - now handled by TaskTool
		self.container = container

		# Get session state from orchestrator (single source of truth)
		self.session_id = orchestrator.session_id
		self.user_id = orchestrator.user_id
		self.workspace_dir = orchestrator.workspace_dir

		# S-1: session's matched skills, keyed by id, for the load_skill tool
		# (progressive disclosure). Populated during agent construction.
		self._session_skills = {}

		# Task 12: session-scoped set of skill_ids already delivered via load_skill,
		# so a repeated load_skill(skill_id) call in the same session short-circuits
		# to a short "already active" ack instead of re-emitting the full body. See
		# tools/controller/_helpers.py::build_load_skill_result.
		self._activated_skills: set = set()

		# Tool management data structures
		# Registry is the single source of truth for actions
		self._tools: Dict[str, ToolInfo] = {}
		self._lock = Lock()
		self._action_list_cache: Optional[List[str]] = None
		self._tool_list_cache: Optional[List[str]] = None

		# Create controller logger with session context
		from agents.task.logging_config import get_task_logger
		self.logger = get_task_logger("controller", self.session_id)
		
		# Initialize libraries needed
		try:
			from agents.task.loop_types.controller_types import ActionResult
			self.ActionResult = ActionResult
		except ImportError:
			# Define fallback ActionResult class
			class ActionResult:
				"""Fallback ActionResult implementation for Controller."""
				def __init__(self, extracted_content=None, error=None, include_in_memory=False, is_done=False):
					self.extracted_content = extracted_content
					self.error = error
					self.include_in_memory = include_in_memory
					self.is_done = is_done
		self.ActionResult = ActionResult
		
		self.logger.debug(f"Using workspace directory: {self.workspace_dir}")

		# Ensure the directory exists (orchestrator should have created it, but verify)
		os.makedirs(self.workspace_dir, exist_ok=True)
		
		# Store the available file paths
		self.available_file_paths = available_file_paths or []

		# Create registry for actions
		try:
			from tools.controller.registry.service import Registry
		except ImportError:
			# Try alternative import path
			try:
				from tools.controller.registry.service import Registry
			except ImportError:
				raise ImportError("Could not import Registry class - ensure controller is properly installed")
		
		# Initialize Registry (no circular reference)
		# Issue #4 Fix: Enable strict mode to catch action name collisions early
		self.registry = Registry(
			exclude_actions=self.exclude_actions,  # Use processed value (defaults to [])
			output_model=output_model,
			session_id=self.session_id,  # Already set from orchestrator
			enforce_execution_context=True  # Strict mode: fail on collisions instead of warning
		)

		# NOTE: tool_ids loading moved to async initialize() pattern
		# Tools should be loaded via await controller.load_tools_from_container(tool_ids)
		# after Controller is created, not in __init__
		if tool_ids and container:
			self.logger.warning("tool_ids provided in __init__ - use await load_tools_from_container() instead")

		# Load tools from provided dictionary (synchronous tools dict still supported)
		if tools:
			for name, tool in tools.items():
				self.add_tool(name, tool)
		
		# Track registered actions
		self._registered_actions = {}      # Map of action name to tool name

		# Track operation attempts to prevent infinite retry loops
		self._operation_attempts: Dict[str, int] = {}
		self._max_operation_retries: int = 5  # Default retry limit
		# Higher limits for specific tool types that may have flaky network operations
		self._tool_retry_limits: Dict[str, int] = {
			'polymarket': 8,  # Polymarket MCP subprocess can be slow to initialize
			'mcp': 8,  # MCP tools can have network latency
			'browser': 5,  # Browser operations can be flaky
		}

		# W5 (D3): the ONE policy hook chain — every pre/post hook a Controller
		# carries, in order, is built by tools/controller/policy_chain.py (the agent
		# tier adds its session gates through the same module). The hooks are
		# registered HERE, in the ctor, so a delegated sub-agent's fresh Controller
		# gets every one of them. Seams pinned by source tests:
		# make_effect_record_hook(self) / make_effect_gate_hook(self) live there.
		# Hook pipeline (Item 7E/7H): owns the pre/post/transform hook lists + the
		# fail-mode execution engine (extracted to tools/controller/hooks.py). The
		# _pre/_post/_transform_tool_call_hooks attributes proxy into it; the public
		# register_*/_run_* methods below delegate. Empty => no-op.
		self._hooks_pipeline = HookPipeline(self.logger)
		from tools.controller.policy_chain import install_controller_policy
		install_controller_policy(self)

		# Register only core 'done' action
		self._register_default_actions()
		# 031: the owner-only pause/resume action — unconditional (not a tool_id),
		# in its own module (action_registration.py is at its size ceiling).
		from tools.controller.autonomy_control_action import register_autonomy_control_action
		register_autonomy_control_action(self)
		# 2026-09-15: the agent's own face/voice (read + workspace attach), gated
		# AVATAR_TOOL_ENABLED. Same reason it lives here rather than in
		# action_registration.py: that file is AT its size ceiling.
		from tools.controller.avatar_action import register_avatar_action
		register_avatar_action(self)
		# 2026-09-15 (owner rail "fix tg chat reading"): read-only access to the
		# group ledger for allowlisted rooms, gated GROUP_CHAT_ENABLED. Same
		# action_registration.py size-ceiling escape hatch.
		from tools.controller.room_read_action import register_room_read_action
		register_room_read_action(self)
		# 2026-10-05: the agent's moderation seat (mute/ban/delete) on the same
		# `room_moderator` adapter the owner's /mute /ban use. Same gate and hatch.
		from tools.controller.room_moderate_action import register_room_moderate_action
		register_room_moderate_action(self)
		# F9 (063 WS-4): the `tool_call` bridge, gated TOOL_SCHEMAS_FROZEN. Must be
		# registered BEFORE the first schema emit — the Registry only arms the
		# freeze when the bridge exists, or a late tool would be unreachable. Own
		# module for the same reason as the three above: action_registration.py is
		# at its size ceiling.
		self._register_tool_call_bridge()
		# 061: a decision an autonomous run needs is a durable ASK (any session,
		# any toolset — the exit-rail cron loads no `goal` tool). Same escape hatch.
		from tools.controller.owner_ask_action import register_owner_ask_action
		register_owner_ask_action(self)
		# 2026-09-27: action cards from the agent (present_choice / propose_action).
		# Same escape hatch as owner_ask.
		from tools.controller.card_actions import register_card_actions
		register_card_actions(self)

		# NOTE: Backward compat aliases are registered LAZILY after task tool loads
		# NOT here in __init__ - aliases to non-existent actions cause confusion
		# See load_tools_from_container() and add_tool() for when aliases are created

		# Skip redundant registration - tools are already registered via add_tool
		# which calls registry.wrap_function for each action

		# Log registered actions
		self.logger.info(f"✨ Controller initialized with {self.registry.get_action_count()} actions")

	# REMOVED: _register_todo_actions method - functionality moved to TaskTool
	# REMOVED: _ensure_normalize_path_exists - monkey-patched a _normalize_path
	# nothing ever called on the Controller (file ops use FileSystem._normalize_path)

	# ===== Tool Management Methods =====


	# REMOVED: Auto-registration code and related JSON schema conversion methods
	# MCP tools use simple discovery pattern:
	#   1. Agent calls mcp_list_tools to discover available tools
	#   2. Agent calls mcp_execute_tool(server_name, tool_name, arguments)
	# No auto-registration, no schema conversion - clean and simple!



	@property
	def _mcp_registrar(self) -> MCPActionRegistrar:
		"""Lazily-created MCP→direct-action registrar (survives bare callers)."""
		reg = self.__dict__.get('_mcp_registrar_obj')
		if reg is None:
			reg = MCPActionRegistrar(self)
			self.__dict__['_mcp_registrar_obj'] = reg
		return reg

	async def _register_mcp_tools_as_direct_actions(self, mcp_tool: Any) -> None:
		"""Delegate to MCPActionRegistrar (Item 7H). See tools/controller/mcp_registrar.py."""
		return await self._mcp_registrar.register(mcp_tool)

	def _create_param_model_from_schema(self, action_name: str, schema: Dict[str, Any]) -> Type[BaseModel]:
		"""Delegate to MCPActionRegistrar (Item 7H). See tools/controller/mcp_registrar.py."""
		return self._mcp_registrar.create_param_model(action_name, schema)









	# -- Hook pipeline delegation (Item 7E/7H) -------------------------------
	# The pre/post/transform lists + fail-mode engine live in HookPipeline
	# (tools/controller/hooks.py). These proxies keep the historical attribute
	# names + public register_*/_run_* surface working unchanged (incl. callers
	# that build a bare Controller and set ``_pre_tool_call_hooks = []`` directly).

	@property
	def _hooks(self) -> HookPipeline:
		"""Lazily-created hook pipeline (survives bare ``object.__new__`` callers)."""
		hp = self.__dict__.get('_hooks_pipeline')
		if hp is None:
			hp = HookPipeline(getattr(self, 'logger', None))
			self.__dict__['_hooks_pipeline'] = hp
		return hp

	@property
	def _pre_tool_call_hooks(self):
		return self._hooks.pre

	@_pre_tool_call_hooks.setter
	def _pre_tool_call_hooks(self, value):
		self._hooks.pre = value if value is not None else []

	@property
	def _post_tool_call_hooks(self):
		return self._hooks.post

	@_post_tool_call_hooks.setter
	def _post_tool_call_hooks(self, value):
		self._hooks.post = value if value is not None else []

	@property
	def _transform_tool_result_hooks(self):
		return self._hooks.transform

	@_transform_tool_result_hooks.setter
	def _transform_tool_result_hooks(self, value):
		self._hooks.transform = value if value is not None else []

	def _deny_all_fallback(self, tools, lane: str) -> None:
		"""Security analysis 2026-09-23 (Low): an approval lane whose wiring RAISED
		used to leave its tools UNGATED with only an error log. Fail closed: deny
		every one of them this session, naming the fault, rather than run a money
		or gated verb with no approval at all."""
		gated = frozenset(t for t in (tools or ()) if t)
		if not gated:
			return
		reason = (f"denied: the {lane} could not be wired this session, so this "
		          "action has no approval path — refusing rather than running it "
		          "ungated (see the controller error log)")

		def _deny(action_name, params, context):
			return reason if action_name in gated else None

		try:
			self.register_pre_tool_call_hook(_deny, fail_mode="closed")
			self.logger.error(f"🔒 {lane} unwired — deny-all fallback for {sorted(gated)}")
		except Exception as e:  # nothing left to fall back to; make it loud
			self.logger.critical(f"{lane}: deny-all fallback could not register: {e}")

	def _deny_money_fallback(self, lane: str) -> None:
		"""CR-L20: the payment lane could not even RESOLVE its tool list, so
		:meth:`_deny_all_fallback` has nothing to name. Deny every action the
		capability table classifies as money (``core.wallet.authority.
		money_action``) instead; a probe error there denies too (fail closed)."""
		reason = (f"denied: the {lane} failed this session, so no money action "
		          "has an approval path — refusing rather than running it "
		          "ungated (see the controller error log)")

		def _deny(action_name, params, context):
			try:
				from core.wallet.authority import money_action
				return reason if money_action(str(action_name)) else None
			except Exception:
				return reason

		try:
			self.register_pre_tool_call_hook(_deny, fail_mode="closed")
			self.logger.error(f"🔒 {lane} failed — deny-all fallback for money actions")
		except Exception as e:  # nothing left to fall back to; make it loud
			self.logger.critical(f"{lane}: money deny fallback could not register: {e}")

	def register_pre_tool_call_hook(self, hook: Callable, fail_mode: str = "open") -> None:
		"""Register a pre-tool-call hook.

		hook signature: ``(action_name: str, params: dict, context) -> Optional[str]``.
		Returning a non-empty string DENIES the action with that reason; returning
		None/'' allows it. Hooks run in registration order before each action
		executes — the seam for billing checks, approval, and allow/deny lists.

		fail_mode: ``"open"`` (default, legacy) swallows a raising hook and allows
		the action; ``"closed"`` (guardrail) treats a raising hook as a DENY.
		"""
		self._hooks.register_pre(hook, fail_mode)

	async def _run_pre_tool_call_hooks(self, action_name, params, context):
		"""Run pre-tool-call hooks; return the first denial reason, or None to allow."""
		return await self._hooks.run_pre(action_name, params, context)

	def register_post_tool_call_hook(self, hook: Callable, fail_mode: str = "open") -> None:
		"""Register a post-tool-call hook (observe-only, roadmap P2 / Reference §23).

		hook signature: ``(action_name: str, params: dict, result, context) -> None``.
		Runs after each action executes; its return value is ignored. Use for
		billing reconciliation, metrics, audit.

		fail_mode: ``"open"`` (default) swallows a raising hook; ``"closed"``
		(e.g. billing) propagates the exception so the failure cannot pass silently.
		"""
		self._hooks.register_post(hook, fail_mode)

	async def _run_post_tool_call_hooks(self, action_name, params, result, context):
		"""Run post-tool-call hooks. fail_mode=open swallows; closed re-raises."""
		return await self._hooks.run_post(action_name, params, result, context)

	def register_transform_tool_result_hook(self, hook: Callable, fail_mode: str = "open") -> None:
		"""Register a transform-tool-result hook (roadmap P2 / Reference §23).

		hook signature: ``(action_name, params, result, context) -> Optional[result]``.
		Returning an ActionResult REPLACES the result; returning None keeps the
		current one. Hooks chain in registration order (each sees the prior's output).

		fail_mode: ``"open"`` (default) skips a raising hook (preserves last good
		result); ``"closed"`` replaces the result with an error ActionResult.
		"""
		self._hooks.register_transform(hook, fail_mode)

	async def _run_transform_tool_result_hooks(self, action_name, params, result, context):
		"""Run transform hooks in order, chaining replacements."""
		return await self._hooks.run_transform(action_name, params, result, context)















