"""Agent-construction helpers (W9 2026-10-10: split out of ``construction.py``).

The small, separately testable pieces ``AgentConstructionMixin.__init__`` calls or
that later code reads: the surface profile, room policy/style, verbosity, project
context, tool-call-tracker restore, the save-path normaliser, memory profiling, the
pure parameter validator and the self-only runtime bookkeeping. ``AgentConstructionMixin``
inherits them, so ``AgentConstructionMixin._load_project_context`` etc. still resolve.
"""
from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime

from core.env import bool_env
from core.exceptions import ValidationError as ROBValidationError
from agents.task.constants import LoopDetectionConfig
from agents.task.agent.core.run_state import LLMCallState


class AgentConstructionHelpersMixin:
	"""Helpers of the Agent constructor (see module docstring)."""

	def _resolve_surface_profile(self):
		"""The bound chat surface's capabilities for the <surface> prompt block (G6).

		Resolved through the ONE binding seam (``core.surfaces.binding``), which
		reads the session_chat_registry the orchestrator was bound to before
		``initialize()``. Returns None off a chat surface (goal/cron/`polyrob run`/
		raw API) — the prompt then stays byte-identical to the pre-G6 build.
		Fail-open: a resolution fault must never break agent construction.
		"""
		try:
			from core.surfaces.binding import surface_profile
			profile = surface_profile(self.orchestrator)
			if profile is None:
				# DATA-10: a RESUMED correspondent session binds no chat key, so
				# no profile resolves — and an unbound session reads as the
				# owner's DM. A correspondent-facing session is never that.
				from agents.task.session_class import correspondent_facing
				if correspondent_facing(self.orchestrator):
					return {"surface_id": "chat", "correspondent": True}
			return profile
		except Exception as e:
			self.logger.debug(f"surface profile unresolved (non-fatal): {e}")
			return None

	def _room_policy(self):
		"""The bound ROOM's ``chat.*`` overlay (044 T17), or None off a room.

		A room's style is the ROOM's, not the owner's: his private
		``style.tone``/``style.verbosity`` describe how he wants to be spoken to
		in his DM, and a public room full of other humans is a different
		audience. Resolved once at construction (session-stable, so the prompt
		cache holds) and fail-open — an unreadable policy costs the room its
		style, never the session.
		"""
		cached = getattr(self, "_room_policy_resolved", False)
		if cached:
			return self._room_policy_value
		self._room_policy_resolved = True
		self._room_policy_value = None
		orch = getattr(self, "orchestrator", None)
		try:
			from core.surfaces.room_policy import is_public_session
			if not is_public_session(orch):
				return None
			from core.runtime_paths import data_dir_or_home
			from core.surfaces.binding import surface_profile
			from core.surfaces.chat_policy import load_for_chat
			profile = surface_profile(orch) or {}
			chat_id = str(profile.get("chat_id") or "")
			if not chat_id:
				return None
			_cfg = getattr(getattr(orch, "container", None), "config", None)
			self._room_policy_value = load_for_chat(
				data_dir_or_home(getattr(_cfg, "data_dir", None)),
				str(profile.get("surface_id") or ""), chat_id)
			return self._room_policy_value
		except Exception as e:
			self.logger.debug(f"room policy unresolved (non-fatal): {e}")
			return None

	def _room_style_block(self) -> str:
		"""The ROOM's style line for SELF_CONTEXT (044 T17), or "".

		A public session deliberately carries none of the owner's private
		contract or style (they describe his DM, not a room full of other
		humans), which left a room with no style line at all. This gives the room
		its own — rendered by the SAME `core.prefs.render_style_line`, so a room
		tone is validated, threat-scanned and formatted exactly like the owner's.
		"""
		policy = self._room_policy()
		if policy is None:
			return ""
		try:
			from core.prefs import render_style_line
			fields = {}
			if policy.verbosity:
				fields["style.verbosity"] = policy.verbosity
			if policy.language:
				fields["style.language"] = policy.language
			if policy.tone:
				fields["style.tone"] = policy.tone
			return render_style_line(fields) if fields else ""
		except Exception as e:
			self.logger.debug(f"room style line unresolved (non-fatal): {e}")
			return ""

	def _resolve_verbosity(self):
		"""The reply-length budget for the <message-shape> block (C2).

		In a ROOM the room's own ``chat.verbosity`` decides — and a room with none
		set gets the prompt default, NEVER the owner's private ``style.verbosity``
		(that pref describes his DM, not a room; see ``_room_policy``). Everywhere
		else the owner's ``style.verbosity`` pref decides. ``core.prefs`` is the SSOT for the
		VALUE in both cases; the prompt owns how it is spoken to the model.
		Resolved once here (session-stable) so the system prompt stays
		byte-identical across steps and the prompt cache holds. Returns None when
		nothing is set — the prompt then uses its "normal" default and the build
		is unchanged. Fail-open: a prefs fault must never break agent construction.
		"""
		try:
			from core.surfaces.room_policy import is_public_session
			if is_public_session(getattr(self, "orchestrator", None)):
				room = self._room_policy()
				if room is not None and room.verbosity:
					return str(room.verbosity)
				return None
			from core.instance import resolve_instance_id
			from core.prefs import load_preferences
			from core.runtime_paths import data_dir_or_home
			_container = getattr(getattr(self, "orchestrator", None), "container", None)
			_cfg = getattr(_container, "config", None)
			_data_dir = data_dir_or_home(getattr(_cfg, "data_dir", None))
			_uid = getattr(getattr(self, "orchestrator", None), "user_id", None)
			prefs = load_preferences(_data_dir, _uid, resolve_instance_id())
			value = prefs.get("style.verbosity")
			return str(value) if value else None
		except Exception as e:
			self.logger.debug(f"verbosity pref unresolved (non-fatal): {e}")
			return None

	def _load_project_context(self) -> None:
		"""Auto-load the frozen PROJECT_CONTEXT foundation message (C9, P7 finalization:
		extracted from __init__).
		  - Local CLI single-owner: loaded TRUSTED (steering), gated on
		    project_context_autoload() (default ON under POLYROB_LOCAL).
		  - Server: loaded ONLY under project_context_server_mode(), then injected
		    UNTRUSTED-WRAPPED (framed as DATA) since the repo may be one merely opened.
		Fully fail-open — any error loads nothing. Server byte-identical by default."""
		from core.surfaces.room_policy import is_public_session
		if is_public_session(getattr(self, "orchestrator", None)):
			return
		try:
			from core.config_policy import AutonomyConfig, local_mode_enabled
			from agents.task.agent.core.project_context import build_project_context_message
			_local = local_mode_enabled()
			# Server tier searches the tenant's session workspace, NEVER the process
			# CWD (install dir); on any failure leave it None so server-mode loads nothing.
			# C-2: a local process running FROM the code root (the service under
			# POLYROB_LOCAL=1, cwd /opt/polyrob) takes the workspace too.
			from agents.task.agent.core.project_context import is_code_root_dir
			_cwd = os.getcwd()
			_workspace_dir = None
			if not _local or is_code_root_dir(_cwd):
				try:
					from agents.task.path import pm
					_workspace_dir = str(pm().get_workspace_dir(
						self.orchestrator.session_id,
						getattr(self.orchestrator, "user_id", None),
					))
				except Exception:
					_workspace_dir = None
			# F25: the cap follows the MODEL WINDOW (4 % of it, clamped to 4 K–40 K)
			# instead of a fixed 20 000 tokens — 20 K was 10 % of a 200 K window and
			# 2 % of a 1 M one. An operator who PINNED PROJECT_CONTEXT_MAX_TOKENS to
			# anything other than the default still wins: we only offer the window
			# when the flag is at its default.
			from agents.task.agent.core.project_context import (
				DEFAULT_PROJECT_CONTEXT_CAP_TOKENS,
			)
			_cap_tokens = AutonomyConfig.project_context_max_tokens()
			_context_window = None
			if _cap_tokens == DEFAULT_PROJECT_CONTEXT_CAP_TOKENS:
				_window = getattr(self.message_manager, "max_input_tokens", None)
				if isinstance(_window, int) and _window > 0:
					_context_window = _window
			_proj_ctx = build_project_context_message(
				local=_local,
				autoload=AutonomyConfig.project_context_autoload(),
				server_mode=AutonomyConfig.project_context_server_mode(),
				cwd=_cwd,
				workspace_dir=_workspace_dir,
				cap_tokens=_cap_tokens,
				context_window=_context_window,
			)
			self.message_manager.set_project_context_message(_proj_ctx)
		except Exception as e:
			self.logger.debug(f"Could not load project context (non-fatal): {e}")

	def _restore_tool_call_tracker(self, orchestrator) -> None:
		"""Restore saved tool-call-tracker state from a previous session (P7
		finalization: extracted from __init__). Best-effort — reads self.session_id /
		self.tool_call_tracker, fail-open on any error."""
		if not (self.session_id and orchestrator):
			return
		try:
			from agents.task.path import pm
			tool_calls_file = pm().create_file_path(
				session_id=self.session_id,
				subdir_name="data",
				filename="tool_calls.json",
				user_id=orchestrator.user_id if hasattr(orchestrator, 'user_id') else None
			)
			if tool_calls_file.exists():
				if self.tool_call_tracker.load_from_file(tool_calls_file):
					self.logger.info("📂 Restored tool call tracker state from previous session")
		except Exception as e:
			self.logger.debug(f"No tool call tracker state to restore: {e}")

	def _normalize_save_conversation_path(self, save_conversation_path) -> None:
		"""Normalize + store save_conversation_path via the path manager (P7
		finalization: extracted from __init__). Sets self.save_conversation_path to a
		pm()-resolved logs path, or None when unset."""
		if save_conversation_path:
			from agents.task.path import pm
			self.save_conversation_path = str(pm().create_file_path(
				self.orchestrator.session_id,
				"logs",
				os.path.basename(save_conversation_path) or "conversation",
				user_id=self.orchestrator.user_id,
			))
			self.logger.info(f"Using normalized conversation path: {self.save_conversation_path}")
		else:
			self.save_conversation_path = None

	def _setup_memory_profiling(self) -> None:
		"""Optional tracemalloc profiling behind PROFILE_MEM=1 (P7 finalization:
		extracted from __init__). Self-contained — reads only self.logger and
		registers an atexit dump; a no-op when the flag is off."""
		if not bool_env("PROFILE_MEM", False):
			return
		try:
			import tracemalloc
			import atexit
			import pprint

			tracemalloc.start()
			self.logger.info("Memory profiling enabled - tracemalloc started")

			@atexit.register
			def _dump_memory_stats():
				try:
					top = tracemalloc.take_snapshot().statistics("lineno")[:20]
					self.logger.info("Top 20 memory allocations:")
					pprint.pprint(top)
				except Exception as e:
					self.logger.warning(f"Error dumping memory stats: {e}")
		except ImportError:
			self.logger.warning("tracemalloc not available for memory profiling")
		except Exception as e:
			self.logger.warning(f"Failed to set up memory profiling: {e}")

	@staticmethod
	def _validate_construction_params(*, max_failures, retry_delay, max_input_tokens,
	                                  max_actions_per_step, max_error_length, task, orchestrator):
		"""Validate critical construction parameters (P7 finalization: extracted from
		__init__). Pure — raises ROBValidationError on invalid input, sets no state."""
		if not isinstance(max_failures, int) or max_failures < 0:
			raise ROBValidationError(f"max_failures must be a non-negative integer, got {max_failures}")
		if not isinstance(retry_delay, int) or retry_delay < 0:
			raise ROBValidationError(f"retry_delay must be a non-negative integer, got {retry_delay}")
		if max_input_tokens is not None and (not isinstance(max_input_tokens, int) or max_input_tokens <= 0):
			raise ROBValidationError(f"max_input_tokens must be a positive integer or None, got {max_input_tokens}")
		if not isinstance(max_actions_per_step, int) or max_actions_per_step <= 0:
			raise ROBValidationError(f"max_actions_per_step must be a positive integer, got {max_actions_per_step}")
		if not isinstance(max_error_length, int) or max_error_length <= 0:
			raise ROBValidationError(f"max_error_length must be a positive integer, got {max_error_length}")
		if not task or not isinstance(task, str):
			raise ROBValidationError("task must be a non-empty string")
		if not orchestrator:
			raise ROBValidationError("orchestrator is required - Agent cannot run standalone")

	def _wire_runtime_state(self) -> None:
		"""Self-only runtime bookkeeping (B03, S7 2026-08-29): stall-detection counters,
		the per-run ``session_data`` dict, the native-tools debug switch and the
		loop-detection deques/thresholds. Extracted verbatim from ``__init__`` — this
		block read no constructor locals, so it is safe to hoist; the ``locals()``-
		consuming profile block above it is deliberately NOT extracted."""
		# Stall detection tracking
		self._last_action_count = 0
		self._stall_check_interval = 30.0  # Check every 30 seconds
		self._stall_check_task = None
		# Whether/since when an LLM response is awaited (owns _llm_call_in_progress /
		# _llm_call_start_time, W9 core/run_state.py).
		self.llm_call = LLMCallState()

		# Create a session data dictionary for later use
		self.session_data = {
			"agent_id": self.agent_id,
			"task": self.task,
			"model_name": self.model_name,
			"created_at": datetime.now().isoformat(),
			"steps": [],
			"llm_requests": []
		}

		# Native tools debug flag
		self._native_tools_debug = bool_env('NATIVE_TOOLS_DEBUG', False)
		if self._native_tools_debug:
			self.logger.setLevel(logging.DEBUG)
			self.logger.info("[NATIVE_TOOLS] Debug mode enabled")

		# Loop detection variables with improved thresholds
		self._previous_actions = deque(maxlen=LoopDetectionConfig.MEMORY_WINDOW_SIZE)  # Use consistent window size
		self._recent_action_names = deque(maxlen=LoopDetectionConfig.MEMORY_WINDOW_SIZE)  # names, for _has_active_browser_usage
		self._action_repetition_counter = 0
		self._max_allowed_repetitions = LoopDetectionConfig.MAX_ALLOWED_REPETITIONS
		self._last_browser_states = deque(maxlen=3)  # MODIFIED: Use bounded deque instead of list
		self._unchanged_state_count = 0
		self._state_change_threshold = LoopDetectionConfig.STATE_CHANGE_THRESHOLD
