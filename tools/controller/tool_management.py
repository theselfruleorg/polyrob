"""ToolManagementMixin — Controller tool lifecycle (UP-11, verbatim code-motion)."""
import asyncio
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type, Union

from tools.controller.types import ActionResult
from tools.controller._helpers import ToolInfo


class ToolManagementMixin:
	async def load_tools_from_container(self, tool_ids: List[str]) -> Dict[str, Any]:
		"""
		Load tools from the global DependencyContainer with initialization.

		Args:
			tool_ids: List of tool IDs to load from container

		Returns:
			Dict of tool_id -> tool_instance loaded from container
		"""
		import asyncio
		loaded = {}

		if not self.container:
			self.logger.error("No container available - cannot load tools")
			return loaded

		self.logger.debug(f"Attempting to load tools {tool_ids} from container")

		for tool_id in tool_ids:
			try:
				tool = None
				tool_service_name = f"{tool_id}_tool"

				# Method 1: Try with _tool suffix
				if self.container.has_service(tool_service_name):
					tool = self.container.get_service(tool_service_name)
					if tool:
						self.logger.debug(f"Found tool '{tool_id}' as '{tool_service_name}'")

				# Method 2: Try without suffix
				if not tool and self.container.has_service(tool_id):
					tool = self.container.get_service(tool_id)
					if tool:
						self.logger.debug(f"Found tool '{tool_id}' directly")

				# Method 3: Special handling for browser
				if not tool and tool_id == 'browser':
					# Try browser_manager path
					if self.container.has_service('browser_manager'):
						browser_manager = self.container.get_service('browser_manager')
						if browser_manager and hasattr(browser_manager, 'browser'):
							browser = browser_manager.browser
							if browser:
								tool = browser
								self.logger.debug("Got browser from browser_manager")

				# Add the tool if found
				if tool:
					# ENSURE TOOL IS INITIALIZED BEFORE USE
					if hasattr(tool, 'is_initialized'):
						if not tool.is_initialized:
							self.logger.info(f"Initializing tool '{tool_id}' before loading")
							if hasattr(tool, 'initialize') and callable(tool.initialize):
								# Check if initialize is async
								if asyncio.iscoroutinefunction(tool.initialize):
									await tool.initialize()
								else:
									tool.initialize()
								self.logger.info(f"✓ Initialized tool '{tool_id}'")
							else:
								self.logger.warning(f"Tool '{tool_id}' has no initialize method")

					self.add_tool(tool_id, tool)
					loaded[tool_id] = tool
					self.logger.debug(f"✓ Loaded tool '{tool_id}' from container")

					# MCP tool: Register individual MCP tools as direct actions
					# This eliminates the nested arguments problem that LLMs struggle with
					if tool_id == 'mcp':
						self.logger.info("✅ MCP tool loaded - registering individual MCP tools as direct actions")
						await self._register_mcp_tools_as_direct_actions(tool)
				else:
					self.logger.warning(f"✗ Tool '{tool_id}' not found in container")

			except Exception as e:
				self.logger.error(f"Failed to load tool '{tool_id}': {e}", exc_info=True)

		# Register backward compat aliases AFTER tools are loaded
		# This ensures aliases point to actual registered actions
		self._register_backward_compat_aliases()

		return loaded

	def add_tool(self, name: str, tool: Any) -> Dict[str, Callable]:
		"""
		Register a tool instance with the controller.

		Args:
			name: Name/ID of the tool
			tool: Tool instance

		Returns:
			Dictionary of registered actions from this tool
		"""
		with self._lock:
			# Clear caches
			self._action_list_cache = None
			self._tool_list_cache = None

			# Configure tool with session_id and workspace BEFORE extracting actions
			self._configure_tool(name, tool)

			# Extract actions from tool (after configuration)
			# Registry.extract_tool_actions() returns actions with metadata but doesn't register them
			actions = self.registry.extract_tool_actions(name, tool)

			# Create tool info
			tool_info = ToolInfo(
				instance=tool,
				actions=actions,
				name=name
			)

			# Check if tool already exists
			if name in self._tools:
				old_info = self._tools[name]
				# If it's the exact same instance, skip re-registration
				if old_info.instance is tool:
					self.logger.debug(f"Tool '{name}' already registered with same instance, skipping")
					return old_info.actions
				self.logger.debug(f"Tool '{name}' already registered, updating with new instance")

			# Register new tool
			self._tools[name] = tool_info

			self.logger.info(
				f"Registered tool '{name}' with {len(actions)} actions: "
				f"{list(actions.keys())[:5]}{'...' if len(actions) > 5 else ''}"
			)

			# Register actions with Registry for schema generation
			if hasattr(self, 'registry') and self.registry:
				for action_name, action_info in actions.items():
					try:
						# Validate: Reject flat names without proper namespacing
						if '_' not in action_name and name:
							# This is a flat name that needs namespacing
							self.logger.debug(f"Action '{action_name}' from tool '{name}' will be namespaced")

						# Handle both callable actions and action dictionaries
						if isinstance(action_info, dict):
							# Browser and other tools return dictionaries with action info
							action_func = action_info.get('function')
							description = action_info.get('description', f"Execute {action_name} from {name} tool")
							param_model = action_info.get('param_model')
						else:
							# Simple callable actions
							action_func = action_info
							description = (
								getattr(action_func, '_description', None) or
								getattr(action_func, '__doc__', None) or
								f"Execute {action_name} from {name} tool"
							)
							param_model = getattr(action_func, '_param_model', None)

						# Use Registry's wrap_function to properly register the action.
						# P2-18: namespace to prevent collisions, but DON'T double-prefix a
						# method that already carries the tool name (e.g. tool `perplexity`
						# method `perplexity_search` -> keep `perplexity_search`, not
						# `perplexity_perplexity_search`). The old unconditional prefix
						# produced ugly schema names (perplexity_perplexity_search,
						# anysite_anysite_api, goal_goal_list) that differed from the names
						# the prompt/skills teach, leaving only the collision-prone fuzzy
						# suffix match to rescue calls. The fuzzy match STAYS as a compat
						# shim for sessions/histories that reference the old double name.
						if action_name == name or action_name.startswith(f"{name}_"):
							namespaced_name = action_name
						else:
							namespaced_name = f"{name}_{action_name}"

						# Validate the namespaced name doesn't conflict
						existing = self.registry.get_action(namespaced_name)
						if existing and existing.tool != name:
							self.logger.error(
								f"Action name collision: '{namespaced_name}' already registered by tool '{existing.tool}'. "
								f"Skipping registration from tool '{name}'."
							)
							continue

						self.registry.wrap_function(
							name=namespaced_name,
							function=action_func,
							description=description,
							tool=name,
							param_model=param_model
						)
						self.logger.debug(f"Registered action {namespaced_name} with Registry")
					except Exception as e:
						self.logger.warning(f"Failed to register action {name}_{action_name} with Registry: {e}")

			# Register backward compat aliases when task tool is loaded
			# This ensures todo_list -> task_todo_list aliases work
			if name == 'task':
				self._register_backward_compat_aliases()

			return actions

	def _configure_tool(self, tool_name: str, tool: Any) -> None:
		"""Configure a tool with session_id and workspace.

		Args:
			tool_name: Name of the tool
			tool: Tool instance to configure
		"""
		# Set session_id on the tool
		if self.session_id:
			session_id_set = False
			try:
				if hasattr(tool, 'session_id'):
					tool.session_id = self.session_id
					session_id_set = True
				elif hasattr(tool, 'set_session_id') and callable(getattr(tool, 'set_session_id')):
					tool.set_session_id(self.session_id)
					session_id_set = True

				if session_id_set:
					self.logger.debug(f"✓ Set session_id on tool {tool_name}")
				else:
					# Only warn if tool has actions that might need session_id
					# This check happens after action extraction, so we skip the warning here
					# The warning is handled in add_tool after we know if there are actions
					pass
			except Exception as e:
				self.logger.error(f"Failed to set session_id on tool {tool_name}: {e}")

		# Set workspace_dir on the tool
		if hasattr(self, 'workspace_dir') and self.workspace_dir:
			try:
				# Try direct attribute
				if hasattr(tool, 'workspace_dir'):
					tool.workspace_dir = self.workspace_dir
					self.logger.debug(f"Set workspace_dir on tool {tool_name}")
				# Try setter method
				elif hasattr(tool, 'set_workspace_dir') and callable(getattr(tool, 'set_workspace_dir')):
					tool.set_workspace_dir(self.workspace_dir)
					self.logger.debug(f"Set workspace_dir on tool {tool_name} via setter")
				# Try container attribute
				elif hasattr(tool, 'container') and tool.container:
					tool.container.workspace_dir = self.workspace_dir
					self.logger.debug(f"Set workspace_dir on {tool_name} container")
			except Exception as e:
				self.logger.warning(f"Could not set workspace_dir on tool {tool_name}: {e}")

		# Set user_id if available
		if hasattr(self, 'user_id') and self.user_id:
			try:
				if hasattr(tool, 'user_id'):
					tool.user_id = self.user_id
					self.logger.debug(f"Set user_id on tool {tool_name}")
			except Exception as e:
				self.logger.debug(f"Could not set user_id on tool {tool_name}: {e}")

	def remove_tool(self, name: str) -> None:
		"""
		Remove a tool and its actions from the controller.

		Args:
			name: Name of the tool to remove
		"""
		with self._lock:
			if name not in self._tools:
				self.logger.warning(f"Tool '{name}' not found, cannot remove")
				return

			# Clear caches
			self._action_list_cache = None
			self._tool_list_cache = None

			# Get tool info
			tool_info = self._tools[name]

			# Remove actions from Registry (single source of truth)
			for action_name in tool_info.actions:
				# P2 finalization: compute the SAME namespaced key registration used
				# (see add_tool ~line 176). remove_tool used to unconditionally prefix
				# `{name}_{action}`, so a tool whose action is ALREADY tool-prefixed
				# (e.g. perplexity / perplexity_search) was never actually removed —
				# list_tools reported it gone while its actions stayed callable.
				if action_name == name or action_name.startswith(f"{name}_"):
					full_name = action_name
				else:
					full_name = f"{name}_{action_name}"
				# Use Registry's thread-safe remove method
				if hasattr(self.registry, 'remove_action'):
					self.registry.remove_action(full_name)
				else:
					# Fallback: log warning - removal not supported
					self.logger.warning(f"Registry doesn't support action removal for {full_name}")

			# Remove tool
			del self._tools[name]

			self.logger.info(f"Removed tool '{name}' and its {len(tool_info.actions)} actions")

	def get_tool(self, name: str) -> Optional[Any]:
		"""
		Get a tool instance by name.

		Args:
			name: Name of the tool

		Returns:
			Tool instance or None if not found
		"""
		with self._lock:
			tool_info = self._tools.get(name)
			return tool_info.instance if tool_info else None

	def has_tool(self, name: str) -> bool:
		"""
		Check if a tool is registered.

		Args:
			name: Name of the tool

		Returns:
			True if tool is registered, False otherwise
		"""
		with self._lock:
			return name in self._tools

	def list_tools(self) -> List[str]:
		"""
		List all registered tools.

		Returns:
			List of tool names
		"""
		with self._lock:
			if self._tool_list_cache is None:
				self._tool_list_cache = list(self._tools.keys())
			return self._tool_list_cache.copy()
