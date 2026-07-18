import os
from typing import Callable, Dict, Optional, Type, Any, List
from dataclasses import dataclass
from threading import Lock

from pydantic import BaseModel

from tools.controller.registry.views import ActionModel
from tools.controller.types import ActionResult
from tools.controller.hooks import HookPipeline
from tools.controller.mcp_registrar import MCPActionRegistrar
from tools.browser.context import BrowserContext
from tools.controller.execution_context import ActionExecutionContext
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


class Controller(ExecutionMixin, ToolManagementMixin, IntrospectionMixin, ActionRegistrationMixin):
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
		# (progressive disclosure). Populated during agent construction when
		# SKILL_PROGRESSIVE_DISCLOSURE is on; empty otherwise.
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
			from tools.controller.types import ActionResult
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

		# owner-UX P1 T5: per-tenant home dir for pref resolution below — mirrors
		# the SELF_CONTEXT block's data_dir source (construction.py):
		# container.config.data_dir, falling back to "data". Resolved once,
		# fail-open, so a Mock/absent container never breaks Controller construction.
		from core.runtime_paths import data_dir_or_home
		try:
			_pref_data_home = data_dir_or_home(
				getattr(getattr(self.container, "config", None), "data_dir", None))
		except Exception:
			_pref_data_home = data_dir_or_home(None)

		# Hook pipeline (Item 7E/7H): owns the pre/post/transform hook lists + the
		# fail-mode execution engine (extracted to tools/controller/hooks.py). The
		# _pre/_post/_transform_tool_call_hooks attributes proxy into it; the public
		# register_*/_run_* methods below delegate. Empty => no-op.
		self._hooks_pipeline = HookPipeline(self.logger)
		_denylist_env = os.getenv("POLYROB_TOOL_DENYLIST", "").strip()
		_denylist = [n.strip() for n in _denylist_env.split(",") if n.strip()]
		# owner-UX P1 T5: `approvals.deny` prefs UNION into the operator denylist —
		# additive only (a pref can ADD a never-run action, never remove one the
		# operator configured via POLYROB_TOOL_DENYLIST). No pref file => the list
		# above is returned unchanged (byte-identical legacy).
		try:
			from core import prefs as _prefs
			_denylist = list(_prefs.resolve(
				"approvals.deny", self.user_id, _pref_data_home,
				env_value=_denylist, default=_denylist,
			) or [])
		except Exception as e:
			self.logger.error(f"denylist pref union skipped (non-fatal): {e}")
		if _denylist:
			self.register_pre_tool_call_hook(
				make_denylist_hook(_denylist),
				fail_mode="closed",  # a crashing guardrail must DENY, not silently allow
			)

		# Approval seam (Item 7E): gate the resolved action set through an
		# ApprovalProvider (default AutoApprover = allow). Empty set => no-op.
		# owner-UX P2 T4: the full composition — the FROZEN env+posture set
		# (WS-6/WS-7: `APPROVAL_REQUIRED_TOOLS`/`APPROVAL_PROVIDER` snapshotted at
		# import so a mid-process env mutation can't flip gating; posture >= 2
		# UNIONs the compute-tool gated set and defaults the provider to
		# interactive) UNIONed with owner `approvals.require` prefs, provider
		# tightened by `approvals.provider` (stricter-of, never looser) — now
		# lives in ONE helper, `effective_approval_state()`
		# (tools/controller/approval.py). The `/approve` REPL command +
		# `polyrob approvals` CLI call the SAME helper for display, so what the
		# owner is shown can never drift from what's enforced here. No pref file
		# => byte-identical to the pre-extraction inline composition.
		from tools.controller.approval import effective_approval_state
		_gates, _provider_name = effective_approval_state(self.user_id, _pref_data_home)
		_required = set(_gates.keys())
		if _required:
			# H9: importing this module registers the 'interactive_cli' provider so an
			# operator can actually select it via APPROVAL_PROVIDER.
			try:
				import tools.controller.approval_interactive  # noqa: F401
			except Exception:
				pass
			from tools.controller.approval import get_approval_provider_or_deny, make_approval_hook
			if _provider_name == "auto_notify":
				# 013 T4: act-and-report under effective AUTONOMY_MODE=autonomous —
				# TWO lanes instead of the single supervised hook. The always-gated
				# self-modification verbs + owner `approvals.require` pref pins stay
				# on the durable, remotely approvable owner_queue provider; the rest
				# of the gated set is allowed by auto_notify and reported post-hoc
				# (audit event + one owner notification per successful run). The
				# supervised path below stays byte-identical.
				try:
					# Importing this module registers the 'owner_queue' provider
					# (mirrors the payment wiring at :240 below).
					import tools.controller.approval_queue  # noqa: F401
					from tools.controller.approval import autonomous_gating_lanes
					from tools.controller.approval_queue import (
						OwnerQueueApprover, make_tool_auto_notify_hook)
					_queued, _reported = autonomous_gating_lanes(_gates)
					# Finding 2 (013 T4 review): PAYMENT_APPROVAL_TOOLS (x402_request,
					# hyperliquid/polymarket order verbs) already get their OWN
					# first-class pre-hook (mode="approve" -> owner_queue) + post-hoc
					# notify (mode="auto") wired below via `payment_approval_mode()` —
					# never ALSO route them through this generic reported lane, or a
					# within-cap payment under PAYMENT_APPROVAL_MODE=auto gets a
					# second, misleading "[auto-approved]" notification even when the
					# owner explicitly approved it (mode="approve"). They stay
					# ELIGIBLE for the owner_queue lane above if they're ever
					# `_ALWAYS_GATED_VERBS`/pref-pinned (they aren't today) — this
					# only strips them out of `_reported`, never `_queued`.
					try:
						from core.config_policy import PAYMENT_APPROVAL_TOOLS as _PAY_TOOLS
						_reported -= set(_PAY_TOOLS)
					except Exception as e:
						self.logger.error(
							f"Failed to exclude PAYMENT_APPROVAL_TOOLS from the "
							f"auto_notify reported lane: {e}")
					_orch = self.orchestrator
					_taint_probe = lambda: bool(  # noqa: E731 — same probe shape as payment wiring
						getattr(_orch, "_correspondent_tainted", False))
					if _queued:
						_q_provider = get_approval_provider_or_deny(
							"owner_queue", user_id=self.user_id, home_dir=_pref_data_home,
						)
						# Same Finding-1 taint short-circuit as the payment wiring:
						# a tainted turn is denied by owner_queue itself — no durable
						# ask, no owner notification.
						if isinstance(_q_provider, OwnerQueueApprover):
							_q_provider.set_taint_probe(_taint_probe)
						self.register_pre_tool_call_hook(
							make_approval_hook(_q_provider, _queued),
							fail_mode="closed",  # approval failure must DENY
						)
					if _reported:
						_r_provider = get_approval_provider_or_deny(
							"auto_notify", user_id=self.user_id, home_dir=_pref_data_home,
						)
						self.register_pre_tool_call_hook(
							make_approval_hook(_r_provider, _reported),
							fail_mode="closed",
						)
						self.register_post_tool_call_hook(
							make_tool_auto_notify_hook(
								self.container, _reported, taint_probe=_taint_probe),
							fail_mode="open",  # a notify failure must never break the caller
						)
					self.logger.info(
						f"🔐 Autonomous approval lanes: owner_queue={sorted(_queued)} "
						f"auto_notify={sorted(_reported)}"
					)
				except Exception as e:
					self.logger.error(f"Failed to wire autonomous approval lanes: {e}")
			else:
				# H9: fail-CLOSED. An unknown APPROVAL_PROVIDER resolves to deny-by-default
				# (not skip-registration), so a misconfigured provider can never silently
				# leave the requested tools UNGATED.
				# owner-UX P2 T5: thread tenant context so the interactive ladder's
				# [a]lways-allow/[n]ever branches can read/write this tenant's
				# preferences.toml. Providers that don't accept these kwargs
				# (AutoApprover/DenyByDefaultApprover) ignore them (see get_approval_provider).
				_provider = get_approval_provider_or_deny(
					_provider_name, user_id=self.user_id, home_dir=_pref_data_home,
				)
				try:
					self.register_pre_tool_call_hook(
						make_approval_hook(_provider, _required),
						fail_mode="closed",  # approval failure must DENY
					)
					self.logger.info(
						f"🔐 Approval gating enabled for: {sorted(_required)} "
						f"(provider={type(_provider).__name__})"
					)
				except Exception as e:
					self.logger.error(f"Failed to wire approval gating: {e}")

		# Payment approval mode (Task 9 / G-2, T7 review fix): outward-facing
		# payment-CREATION actions (core.config_policy.PAYMENT_APPROVAL_TOOLS)
		# are gated FIRST-CLASS — independent of the generic APPROVAL_REQUIRED_TOOLS/
		# APPROVAL_PROVIDER seam above, which stays exactly as it was for every
		# non-payment tool. PAYMENT_APPROVAL_TOOLS splits into two lanes:
		# PAYMENT_RECEIVE_APPROVAL_TOOLS (x402_request) is eligible for act-and-report
		# under mode="auto"; everything else (the live-trade order verbs) is SPEND-side
		# and ALWAYS keeps owner_queue pre-approval regardless of mode — the hard
		# product line is money-spend/trading is never act-and-report. See
		# agents/task/constants.py::payment_approval_mode / PAYMENT_RECEIVE_APPROVAL_TOOLS.
		try:
			from core.config_policy import (
				PAYMENT_APPROVAL_TOOLS, PAYMENT_RECEIVE_APPROVAL_TOOLS, payment_approval_mode)
			_payment_tools = set(PAYMENT_APPROVAL_TOOLS)
			_receive_tools = _payment_tools & set(PAYMENT_RECEIVE_APPROVAL_TOOLS)
			_spend_tools = _payment_tools - _receive_tools
		except Exception as e:
			self.logger.error(f"Failed to resolve PAYMENT_APPROVAL_TOOLS: {e}")
			_payment_tools = set()
			_receive_tools = set()
			_spend_tools = set()
		if _payment_tools:
			_pay_mode = payment_approval_mode()
			if _pay_mode == "approve":
				try:
					# Importing this module registers the 'owner_queue' provider
					# (mirrors the H9 'interactive_cli' self-registration above).
					import tools.controller.approval_queue  # noqa: F401
					from tools.controller.approval import (
						get_approval_provider_or_deny, make_approval_hook)
					from core.config_policy import payment_approval_timeout_sec
					from tools.controller.approval_queue import OwnerQueueApprover
					_pay_provider = get_approval_provider_or_deny(
						"owner_queue", user_id=self.user_id, home_dir=_pref_data_home,
					)
					# Finding 1 (fix pass 1): give owner_queue the SAME
					# correspondent-taint probe correspondent_gate reads
					# (`orchestrator._correspondent_tainted`) so a tainted turn is
					# denied by owner_queue itself — no durable ask, no owner
					# notification — regardless of hook registration order vs
					# correspondent_gate (registered later, in agent construction).
					# `get_approval_provider_or_deny` is the generic factory (only
					# threads user_id/home_dir) — this Controller.__init__ is the
					# site that actually has the orchestrator reference, so the
					# probe is injected post-construction via the setter seam. Only
					# a real OwnerQueueApprover gets it (a monkeypatched/custom
					# 'owner_queue' provider under test is left untouched).
					if isinstance(_pay_provider, OwnerQueueApprover):
						_orch = self.orchestrator
						_pay_provider.set_taint_probe(
							lambda: bool(getattr(_orch, "_correspondent_tainted", False))
						)
					self.register_pre_tool_call_hook(
						make_approval_hook(_pay_provider, _payment_tools,
						                   timeout=payment_approval_timeout_sec()),
						fail_mode="closed",  # approval failure must DENY
					)
					self.logger.info(
						f"💳 Payment approval mode=approve: {sorted(_payment_tools)} "
						"-> owner_queue"
					)
				except Exception as e:
					self.logger.error(f"Failed to wire payment approval gating (spend lane): {e}")
			else:  # "auto" — spend verbs STILL owner_queue pre-approved (hard line);
				# only the receive-side subset act-and-reports.
				if _spend_tools:
					try:
						# Importing this module registers the 'owner_queue' provider
						# (mirrors the H9 'interactive_cli' self-registration above).
						import tools.controller.approval_queue  # noqa: F401
						from tools.controller.approval import (
							get_approval_provider_or_deny, make_approval_hook)
						from core.config_policy import payment_approval_timeout_sec
						from tools.controller.approval_queue import OwnerQueueApprover
						_pay_provider = get_approval_provider_or_deny(
							"owner_queue", user_id=self.user_id, home_dir=_pref_data_home,
						)
						# Finding 1 (fix pass 1): give owner_queue the SAME
						# correspondent-taint probe correspondent_gate reads
						# (`orchestrator._correspondent_tainted`) so a tainted turn is
						# denied by owner_queue itself — no durable ask, no owner
						# notification — regardless of hook registration order vs
						# correspondent_gate (registered later, in agent construction).
						# `get_approval_provider_or_deny` is the generic factory (only
						# threads user_id/home_dir) — this Controller.__init__ is the
						# site that actually has the orchestrator reference, so the
						# probe is injected post-construction via the setter seam. Only
						# a real OwnerQueueApprover gets it (a monkeypatched/custom
						# 'owner_queue' provider under test is left untouched).
						if isinstance(_pay_provider, OwnerQueueApprover):
							_orch = self.orchestrator
							_pay_provider.set_taint_probe(
								lambda: bool(getattr(_orch, "_correspondent_tainted", False))
							)
						self.register_pre_tool_call_hook(
							make_approval_hook(_pay_provider, _spend_tools,
							                   timeout=payment_approval_timeout_sec()),
							fail_mode="closed",  # approval failure must DENY
						)
						self.logger.info(
							f"💳 Payment approval mode=auto (spend lane): {sorted(_spend_tools)} "
							"-> owner_queue"
						)
					except Exception as e:
						self.logger.error(f"Failed to wire payment approval gating (spend lane): {e}")
				if _receive_tools:
					try:
						from tools.controller.approval_queue import make_payment_auto_notify_hook
						# Finding 1 (fix pass 1): SAME taint short-circuit as the
						# 'approve' branch above — mode=auto emits no owner
						# notification/audit for a tainted turn either.
						_orch = self.orchestrator
						self.register_post_tool_call_hook(
							make_payment_auto_notify_hook(
								self.container, _receive_tools,
								taint_probe=lambda: bool(
									getattr(_orch, "_correspondent_tainted", False)),
							),
							fail_mode="open",  # a notify failure must never break the caller
						)
						self.logger.info(
							f"💳 Payment approval mode=auto: {sorted(_receive_tools)} "
							"-> post-execution owner notify"
						)
					except Exception as e:
						self.logger.error(f"Failed to wire payment auto-notify hook: {e}")

		# Register only core 'done' action
		self._register_default_actions()

		# NOTE: Backward compat aliases are registered LAZILY after task tool loads
		# NOT here in __init__ - aliases to non-existent actions cause confusion
		# See load_tools_from_container() and add_tool() for when aliases are created

		# Skip redundant registration - tools are already registered via add_tool
		# which calls registry.wrap_function for each action

		# Log registered actions
		self.logger.info(f"✨ Controller initialized with {self.registry.get_action_count()} actions")
		
		# Ensure the normalize_path method exists
		self._ensure_normalize_path_exists()






	# REMOVED: _register_todo_actions method - functionality moved to TaskTool

	def _ensure_normalize_path_exists(self):
		"""Ensure the normalize_path method exists and is usable.
		This method is called during initialization to make sure file operations can use path normalization.
		"""
		if not hasattr(self, '_normalize_path'):
			# Define the normalize_path method if it doesn't exist
			def _normalize_path(self, file_path: str) -> str:
				"""Normalize a file path using PathManager.

				This is a simple wrapper that delegates all path normalization
				to the centralized PathManager.

				Args:
					file_path: Path to normalize

				Returns:
					Normalized path within the workspace directory
				"""
				import os
				from agents.task.path import pm

				if not file_path:
					raise ValueError("File path cannot be empty")

				# Ensure session_id is set
				if not hasattr(self, 'session_id') or not self.session_id:
					raise ValueError("No session_id available for path normalization")

				# Delegate to PathManager's normalize_path
				normalized_path = pm().normalize_path(file_path, session_id=self.session_id)

				# Ensure the directory exists for the normalized path
				if normalized_path and normalized_path != file_path:
					os.makedirs(os.path.dirname(normalized_path), exist_ok=True)

				self.logger.debug(f"Normalized path: {file_path} -> {normalized_path}")
				return normalized_path
				
			# Bind the method to the class
			import types
			self._normalize_path = types.MethodType(_normalize_path, self)


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

















