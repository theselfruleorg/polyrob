"""IntrospectionMixin — Controller read-only registry accessors + MCP prompt builders
(UP-11, verbatim code-motion)."""
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type, Union

from tools.controller.registry.views import ActionModel
from tools.controller.types import ActionResult


class IntrospectionMixin:
	def list_actions(self) -> List[str]:
		"""
		List all available actions from Registry.

		Returns:
			List of flat action names from the Registry
		"""
		with self._lock:
			if self._action_list_cache is None:
				# Use Registry as single source of truth for actions
				self._action_list_cache = self.registry.list_actions()
			return self._action_list_cache.copy()

	def get_action(self, action_name: str) -> Optional[Callable]:
		"""
		Get an action callable by name from Registry.

		Args:
			action_name: Flat name of the action

		Returns:
			Action callable or None if not found
		"""
		with self._lock:
			# Use Registry's thread-safe get_action method
			action_info = self.registry.get_action(action_name)
			if action_info and action_info.function:
				return action_info.function

			return None

	def action(self, description: str, **kwargs):
		"""Decorator for registering custom actions"""
		return self.registry.action(description, **kwargs)

	def get_action_schema(self, action_name: str) -> Dict[str, Any]:
		"""Get the expected schema for a specific action.
		
		This method delegates to the Registry to retrieve action schemas,
		which are used for parameter validation and normalization.
		
		Args:
			action_name: Name of the action to get schema for
			
		Returns:
			Dictionary containing field information for the action.
			Returns empty dict if action not found or registry not available.
			
		Example:
			schema = controller.get_action_schema('write_file')
			# Returns: {'file_path': {'required': True, 'type': 'str'}, ...}
		"""
		if hasattr(self, 'registry') and self.registry:
			return self.registry.get_action_schema(action_name)
		
		self.logger.warning(f"Cannot get schema for '{action_name}': Registry not available")
		return {}

	def create_action_model(self):
		"""Create a Pydantic action model from all registered actions.

		This is the high-level API for agents to get an action model.
		Delegates to Registry but provides Controller-level access.

		Returns:
			Pydantic model class with all registered actions as optional fields
		"""
		return self.registry.create_action_model()

	def get_all_actions_for_provider(self, provider: str):
		"""Get all action schemas formatted for a specific LLM provider.

		Returns schemas for all registered actions (native tools like browser, filesystem,
		plus MCP wrapper actions like mcp_execute_tool, mcp_list_tools).

		Note: MCP server tools are accessed via the discovery pattern:
		- mcp_list_tools() returns available MCP server tools
		- mcp_execute_tool(server, tool, args) executes them dynamically

		Args:
			provider: The LLM provider name (openai, anthropic, gemini, etc.)

		Returns:
			List of action schemas in the provider's expected format
		"""
		# Get all registered actions from Registry
		actions = self.registry.get_all_actions_for_provider(provider)
		
		# Log summary without duplicating MCP tools
		mcp_count = sum(1 for name in self.registry.list_action_names() if self.registry.get_action(name) and self.registry.get_action(name).tool == 'mcp')
		regular_count = len(actions) - mcp_count
		
		self.logger.debug(
			f"Returning {len(actions)} actions for provider {provider} "
			f"({regular_count} regular, {mcp_count} MCP, all from Registry)"
		)

		return actions

	def supports_native_tools(self, provider: str) -> bool:
		"""Check if a provider supports native tool/function calling.

		This is the high-level API for agents to check provider capabilities.
		Delegates to Registry but provides Controller-level access.

		Args:
			provider: The LLM provider name

		Returns:
			True if the provider has native tool support
		"""
		return self.registry.supports_native_tools(provider)

	def get_prompt_action_index(self) -> str:
		"""Compact one-line-per-action index for NATIVE tool mode (T1-03).

		The full schemas already ship to the provider in the `tools` param, so the
		prompt only needs an at-a-glance catalog. MCP direct actions registered in
		the registry are included automatically; the MCP server appendix that
		get_prompt_description() adds is NOT duplicated here (the <mcp-tools>
		prompt section already carries it).
		"""
		return self.registry.get_prompt_action_index()

	def get_prompt_description(self) -> str:
		"""Get a description of all actions for the prompt.

		This is the high-level API for agents to get action descriptions.
		Delegates to Registry but provides Controller-level access.
		Also appends MCP server information if MCP tool is loaded.

		Returns:
			Formatted string describing all available actions
		"""
		base_description = self.registry.get_prompt_description()

		# Append MCP server information if MCP tool is loaded
		try:
			if 'mcp' in self._tools:
				mcp_tool = self._tools['mcp'].instance
				if hasattr(mcp_tool, 'server_manager') and mcp_tool.server_manager:
					all_tools = mcp_tool.server_manager.get_all_tools()

					if all_tools:
						mcp_section = ["\n**Available MCP Servers:**"]

						for server_name, tools in all_tools.items():
							if not tools:
								continue

							tool_names = [t.name for t in tools]

							# Show first 10 tools, indicate if there are more
							if len(tool_names) > 10:
								tool_list = ", ".join(tool_names[:10]) + f", ... and {len(tool_names) - 10} more"
							else:
								tool_list = ", ".join(tool_names)

							mcp_section.append(f"- **{server_name}** ({len(tool_names)} tools): {tool_list}")

						mcp_section.append("\n**MCP Usage:** Use mcp_execute_tool(server_name='server', tool_name='tool', arguments={{...}})")
						mcp_section.append("**MCP Discovery:** Use mcp_list_tools(server_name='server') to see full tool details and parameters")

						base_description += "\n".join(mcp_section)
		except Exception as e:
			# Log but don't fail if MCP info can't be appended
			import logging
			logger = logging.getLogger(__name__)
			logger.warning(f"Failed to append MCP server information to prompt: {e}")

		return base_description

	def get_mcp_servers_info(self) -> Dict[str, List[str]]:
		"""Get MCP server information for dynamic prompt generation.

		Returns a dict mapping server names to their tool names.
		This allows prompts to use actual server/tool names instead of hardcoded examples.

		Returns:
			Dict of {server_name: [tool_names]} or empty dict if MCP not available
		"""
		result = {}
		try:
			if 'mcp' in self._tools:
				mcp_tool = self._tools['mcp'].instance

				# Get tools from regular MCP servers via server_manager
				if hasattr(mcp_tool, 'server_manager') and mcp_tool.server_manager:
					all_tools = mcp_tool.server_manager.get_all_tools()
					if all_tools:
						for server_name, tools in all_tools.items():
							if tools:
								result[server_name] = [t.name for t in tools]

				# Also include polymarket if it's in requested_servers
				# Polymarket uses a separate gateway, not server_manager
				if hasattr(mcp_tool, 'requested_servers') and mcp_tool.requested_servers:
					if 'polymarket' in mcp_tool.requested_servers and 'polymarket' not in result:
						# Add polymarket with common tool names for prompt generation
						# The actual tools are discovered via mcp_list_tools
						result['polymarket'] = [
							'search_markets', 'get_market', 'get_market_prices',
							'get_orderbook', 'get_user_positions', 'place_order'
						]
		except Exception as e:
			import logging
			logger = logging.getLogger(__name__)
			logger.warning(f"Failed to get MCP server info: {e}")

		return result

	async def get_polymarket_info(self) -> Dict[str, Any]:
		"""Get Polymarket configuration status for the current user.

		Returns wallet configuration status so the agent knows what
		capabilities are available (read-only vs trading).

		Returns:
			Dict with:
				- available: bool - whether polymarket is loaded
				- demo_mode: bool - True if no wallet configured
				- wallet_configured: bool - True if wallet address is set
				- trading_enabled: bool - True if can execute trades
				- trading_limits: dict - configured limits if available
		"""
		result = {
			'available': False,
			'demo_mode': True,
			'wallet_configured': False,
			'trading_enabled': False,
			'trading_limits': None
		}

		try:
			# Check if polymarket tool is loaded
			if 'polymarket' not in self._tools:
				return result

			polymarket_tool = self._tools['polymarket'].instance
			if not polymarket_tool:
				return result

			result['available'] = True

			# Get credentials for current user
			if hasattr(polymarket_tool, 'db_handler') and polymarket_tool.db_handler:
				if hasattr(polymarket_tool, '_user_id') and polymarket_tool._user_id:
					credentials = await polymarket_tool.db_handler.get_credentials(
						polymarket_tool._user_id
					)
					if credentials:
						result['demo_mode'] = credentials.demo_mode
						result['wallet_configured'] = bool(credentials.wallet_address)
						result['trading_enabled'] = (
							not credentials.demo_mode and
							bool(credentials.wallet_address) and
							bool(credentials.private_key) and
							credentials.enabled
						)
						if credentials.trading_limits:
							result['trading_limits'] = credentials.trading_limits.to_dict()
		except Exception as e:
			self.logger.debug(f"Failed to get polymarket info: {e}")

		return result

	def get_action_names(self) -> List[str]:
		"""Get list of all registered action names.

		This is the high-level API for agents to get action names.
		Delegates to Registry but provides Controller-level access.

		Returns:
			List of action names
		"""
		return self.registry.list_action_names()

	def has_action(self, action_name: str) -> bool:
		"""Check if an action is registered.

		This is the high-level API for agents to check action existence.
		Delegates to Registry but provides Controller-level access.

		Args:
			action_name: Name of the action to check

		Returns:
			True if action is registered, False otherwise
		"""
		return self.registry.has_action(action_name)

	def get_action_details(self, action_name: str) -> Optional[Any]:
		"""Get details for a specific action.

		This is the high-level API for agents to get action details.
		Delegates to Registry but provides Controller-level access.

		Args:
			action_name: Name of the action to get details for

		Returns:
			Action details if found, None otherwise
		"""
		return self.registry.get_action_details(action_name)

	def tool_calls_to_actions(self, tool_calls: List[Dict[str, Any]]) -> List[Any]:
		"""Convert multiple tool calls to ActionModels.

		Validates and converts LLM tool calls to ActionModels using Registry.
		MCP wrapper actions (mcp_execute_tool, mcp_list_tools) are registered
		like any other tool. Individual MCP server tools are called dynamically
		via mcp_execute_tool.

		Args:
			tool_calls: List of tool calls

		Returns:
			List of ActionModel instances ready for execution

		Note:
			After calling this method, use get_last_validation_errors() to check
			if any tool calls failed validation and need error responses.
		"""
		# All tools (MCP and regular) are now in Registry
		# Simple delegation - no special MCP handling needed
		return self.registry.tool_calls_to_actions(tool_calls)

	def get_last_validation_errors(self) -> Dict[str, str]:
		"""Get validation errors from the last tool_calls_to_actions call.

		Returns:
			Dict mapping tool_call_id -> error_message for failed validations.
			Empty dict if no errors or tool_calls_to_actions not called yet.
		"""
		return self.registry.get_last_validation_errors()
