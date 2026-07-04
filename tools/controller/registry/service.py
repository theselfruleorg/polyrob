import asyncio
import os
import threading
from inspect import iscoroutinefunction, signature
from typing import Any, Callable, Dict, List, Optional, Type, TYPE_CHECKING
import logging

# Native types
from modules.llm.adapters import BaseChatModel
from pydantic import BaseModel, Field, create_model

from tools.browser.context import BrowserContext
from tools.controller.registry.views import (
	ActionModel,
	ActionRegistry,
	RegisteredAction,
)

if TYPE_CHECKING:
	from tools.controller.execution_context import ActionExecutionContext
from tools.controller.registry.schema_generators import (
	get_schema_generator,
	ToolSchemaGenerator,
)

# Controller handles all tool management

# Import logging config
from agents.task.logging_config import get_task_logger

# Import telemetry - handle possible import errors
try:
	from agents.task.telemetry import get_telemetry
	from agents.task.telemetry.views import (
		ControllerRegisteredFunctionsTelemetryEvent,
		RegisteredFunction,
	)
except ImportError:
	# Create dummy telemetry if imports fail
	def get_telemetry():
		class DummyTelemetry:
			def capture(self, *args, **kwargs):
				pass
		return DummyTelemetry()
	
	class ControllerRegisteredFunctionsTelemetryEvent:
		def __init__(self, **kwargs):
			pass
			
	class RegisteredFunction:
		def __init__(self, **kwargs):
			pass


class Registry:
	"""Service for registering and managing actions"""

	def __init__(
		self, exclude_actions: Optional[list[str]] = None, output_model: Optional[Type[BaseModel]] = None, session_id: Optional[str] = None, enforce_execution_context: bool = False
	):
		"""Registry of actions that the agent can use.

		Args:
			exclude_actions: List of action names to exclude from the registry.
			output_model: Model to use for action results.
			session_id: Session ID for telemetry purposes.
			enforce_execution_context: If True, require execution_context and disallow legacy parameters.
		"""
		self.exclude_actions = exclude_actions if exclude_actions is not None else []
		self.output_model = output_model
		self.session_id = session_id
		self.enforce_execution_context = enforce_execution_context
		
		# Initialize logger
		self.logger = get_task_logger("registry", session_id)
		
		# Initialize telemetry
		self.telemetry = get_telemetry()

		# Initialize registry with thread safety
		self.registry = ActionRegistry()
		self._registry_lock = threading.RLock()  # Thread-safe registry access for both sync and async

		# Tool-schema memoization: provider schema lists are byte-stable for a given
		# set of registered actions, but were regenerated on every step (~3.7k tokens'
		# worth of work + re-send per call). Cache them keyed on (provider, action-set,
		# exclusions); the key changes whenever actions are added/removed, so it
		# self-invalidates without explicit bumping.
		self._provider_schema_cache: dict = {}

		# Track action registration for logging only
		self._action_registration_logged = set()  # Track logged actions

		# Register default actions
		self._register_default_actions()


	def _set_tool_attribution(self, function: Callable, tool_name: str) -> None:
		"""Set tool attribution on a function (SINGLE source of truth for attribution).

		Handles all the edge cases: bound methods, wrapped functions, etc.
		This is the ONLY place where _tool and _service should be set.

		Args:
			function: The callable to set attribution on
			tool_name: Name of the tool to attribute to
		"""
		import inspect

		# Skip if already set to avoid overwriting
		if hasattr(function, '_tool') and function._tool == tool_name:
			return

		# Try to set attributes, handle cases where it's not possible
		try:
			# For bound methods, we can't set attributes directly
			# Store in a registry instead (future: use WeakKeyDictionary)
			if inspect.ismethod(function):
				# Can't set attributes on bound methods
				# The RegisteredAction object will store this instead
				pass
			else:
				# For regular functions and unbound methods
				function._tool = tool_name
				function._service = tool_name  # Keep for backward compat
		except (AttributeError, TypeError):
			# Some callables don't support attribute assignment
			# RegisteredAction will store the tool name instead
			pass

	def _create_param_model(self, function: Callable) -> Type[BaseModel]:
		"""Creates a Pydantic model from function signature
		
		Handles missing type annotations by providing reasonable defaults.
		"""
		sig = signature(function)
		import inspect

		params = {}
		for name, param in sig.parameters.items():
			# Skip self, browser, LLM, file path, and execution_context parameters
			if name in ['self', 'browser', 'page_extraction_llm', 'available_file_paths', 'execution_context']:
				continue

			# Handle missing type annotations with str as default
			if param.annotation is inspect._empty:
				annotation = str
			else:
				annotation = param.annotation

			# Handle missing default values
			if param.default is inspect._empty:
				params[name] = (annotation, ...)
			else:
				params[name] = (annotation, param.default)
			
		# Create the model with a unique name based on the function
		return create_model(
			f'{function.__name__}_parameters',
			__base__=ActionModel,
			**params,  # type: ignore
		)

	def action(
		self,
		description: str,
		param_model: Optional[Type[BaseModel]] = None,
		tool: Optional[str] = None
	):
		"""Decorator for registering actions
		
		Args:
			description: Description of what the action does
			param_model: Optional Pydantic model for parameters
		tool: Optional tool name to attribute the action to
		"""
		def decorator(func: Callable):
			# Skip registration if action is in exclude_actions
			if self.exclude_actions and func.__name__ in self.exclude_actions:
				return func

			# Create param model from function if not provided
			actual_param_model = param_model or self._create_param_model(func)

			# Wrap sync functions to make them async
			if not iscoroutinefunction(func):
				async def async_wrapper(*args, **kwargs):
					return await asyncio.to_thread(func, *args, **kwargs)

				# Copy the signature and other metadata from the original function
				async_wrapper.__signature__ = signature(func)
				async_wrapper.__name__ = func.__name__
				async_wrapper.__annotations__ = func.__annotations__
				wrapped_func = async_wrapper
			else:
				wrapped_func = func

			action = RegisteredAction(
				name=func.__name__,
				description=description,
				function=wrapped_func,
				param_model=actual_param_model,
				tool=tool
			)
			self.registry.actions[func.__name__] = action
			
			# Log registration with tool attribution if provided
			if tool:
				self.logger.debug(f"Registered action '{func.__name__}' with tool '{tool}'")
			else:
				self.logger.debug(f"Registered action '{func.__name__}'")
			
			return func

		return decorator

	def remove_action(self, action_name: str) -> bool:
		"""Unregister an action by name. Returns True if it existed and was removed.

		Thread-safe and busts the per-provider schema cache so the removed action
		stops appearing in generated tool schemas. Without this, Controller.remove_tool
		was a silent no-op (it probed for a non-existent remove_action), leaving a
		"removed" tool's actions fully registered and callable — a capability-revocation
		hole.
		"""
		with self._registry_lock:
			if action_name not in self.registry.actions:
				return False
			del self.registry.actions[action_name]
			self._action_registration_logged.discard(action_name)
			# Schema cache is keyed on the action-set; bust it so the dropped action
			# is no longer emitted to providers.
			self._provider_schema_cache.clear()
			self.logger.debug(f"Removed action '{action_name}'")
			return True

	async def execute_action(
		self,
		action_name: str,
		params: dict,
		execution_context: Optional['ActionExecutionContext'] = None,
		# Legacy parameters for backward compatibility
		browser: Optional[BrowserContext] = None,
		page_extraction_llm: Optional[BaseChatModel] = None,
		sensitive_data: Optional[Dict[str, str]] = None,
		available_file_paths: Optional[list[str]] = None,
		session_id: Optional[str] = None,
	) -> Any:
		"""Execute a registered action with thread-safe registry access"""

		# Enforce execution context if required
		if self.enforce_execution_context:
			if not execution_context:
				raise ValueError("execution_context is required when enforce_execution_context=True")
			if any([browser, page_extraction_llm, sensitive_data, available_file_paths, session_id]):
				self.logger.debug("Legacy parameters ignored when enforce_execution_context=True")

		# Build execution context from parameters if not provided
		if not execution_context:
			# Import here to avoid circular dependency
			from tools.controller.execution_context import ActionExecutionContext
			execution_context = ActionExecutionContext(
				browser_context=browser,
				session_id=session_id or self.session_id,
				workspace_dir=getattr(self, 'workspace_dir', None),
				available_file_paths=available_file_paths or [],
				sensitive_data=sensitive_data or {},
			)
		else:
			# Override context with explicit parameters if provided
			if browser:
				execution_context.browser_context = browser
			if sensitive_data:
				execution_context.sensitive_data = sensitive_data
			if available_file_paths:
				execution_context.available_file_paths = available_file_paths
			if session_id:
				execution_context.session_id = session_id

		with self._registry_lock:
			if action_name not in self.registry.actions:
				raise ValueError(f'Action {action_name} not found')

			action = self.registry.actions[action_name]
		try:
			# Handle None params - convert to empty dict
			if params is None:
				params = {}
			# WS-1.3: systematic camelCase→snake_case reconciliation against the
			# target model's fields (e.g. kimi-k2.6 emits filePath for file_path).
			# Safe for genuinely-camelCase tool params (MCP): only renames when the
			# snake_case form is an actual field of this action's param model.
			if action.param_model is not None:
				try:
					from agents.task.utils_json import reconcile_field_names_to_model
					params = reconcile_field_names_to_model(
						params, set(action.param_model.model_fields.keys())
					)
				except Exception as _e:
					self.logger.debug(f"Field-name reconciliation skipped: {_e}")
			# Create the validated Pydantic model
			validated_params = action.param_model(**params)

			# Check if the first parameter is a Pydantic model
			sig = signature(action.function)
			parameters = list(sig.parameters.values())
			# Guard against missing/invalid annotations before issubclass
			is_pydantic = False
			if parameters:
				first_anno = parameters[0].annotation
				try:
					is_pydantic = isinstance(first_anno, type) and issubclass(first_anno, BaseModel)
				except TypeError:
					is_pydantic = False
				# PEP 563 / `from __future__ import annotations`: the annotation is a
				# *string* (e.g. "GoalCreateAction"), so the isinstance/issubclass
				# check above is False and we'd wrongly splat the model fields as
				# kwargs into a `(self, params: Model, ...)` signature → TypeError.
				# When the action was registered with an explicit param_model, trust
				# it: if the first param's stringized annotation names that model,
				# route the validated model as a single positional arg.
				if (
					not is_pydantic
					and isinstance(first_anno, str)
					and action.param_model is not None
					and first_anno == action.param_model.__name__
				):
					is_pydantic = True
			parameter_names = [param.name for param in parameters]

			if execution_context.sensitive_data:
				validated_params = self._replace_sensitive_data(validated_params, execution_context.sensitive_data)

			# Check if action accepts execution_context parameter
			if 'execution_context' in parameter_names:
				# Modern approach: pass the whole context
				extra_args = {'execution_context': execution_context}
			else:
				# Legacy approach: pass individual parameters
				if 'browser' in parameter_names and not execution_context.browser_context:
					raise ValueError(f'Action {action_name} requires browser but none provided.')
				if 'page_extraction_llm' in parameter_names and not page_extraction_llm:
					raise ValueError(f'Action {action_name} requires page_extraction_llm but none provided.')
				if 'available_file_paths' in parameter_names and not execution_context.available_file_paths:
					raise ValueError(f'Action {action_name} requires available_file_paths but none provided.')

				# Prepare arguments based on parameter type
				extra_args = {}
				if 'browser' in parameter_names:
					extra_args['browser'] = execution_context.browser_context
				if 'page_extraction_llm' in parameter_names:
					extra_args['page_extraction_llm'] = page_extraction_llm
				if 'available_file_paths' in parameter_names:
					extra_args['available_file_paths'] = execution_context.available_file_paths

			# NOTE: Runtime tool mutation removed per upgrade instructions
			# All per-execution data should flow via execution_context only
			# Tools should not be mutated at runtime

			# EXCEPTION: User context propagation for tools that need it
			# This is NOT mutation - it's telling the tool which user's credentials to use
			# Tools like PolymarketTool and MCPTool require user_id to load correct credentials
			if execution_context and execution_context.user_id:
				# Check if action function is a bound method with set_user_context
				if hasattr(action.function, '__self__'):
					tool_instance = action.function.__self__
					if hasattr(tool_instance, 'set_user_context'):
						tool_instance.set_user_context(execution_context.user_id)
						self.logger.debug(f"Set user context '{execution_context.user_id}' on {action.tool or 'unknown'} tool")

			# Execute the function and explicitly await the result
			if is_pydantic:
				result = await action.function(validated_params, **extra_args)
			else:
				result = await action.function(**validated_params.model_dump(), **extra_args)
				
			# Handle the case where result might be another coroutine (nested async calls)
			import inspect
			if inspect.iscoroutine(result):
				result = await result
				
			return result

		except Exception as e:
			raise RuntimeError(f'Error executing action {action_name}: {str(e)}') from e

	def _replace_sensitive_data(self, params: BaseModel, sensitive_data: Dict[str, str]) -> BaseModel:
		"""Replaces the sensitive data in the params"""
		# if there are any str with <secret>placeholder</secret> in the params, replace them with the actual value from sensitive_data

		import re

		secret_pattern = re.compile(r'<secret>(.*?)</secret>')

		def replace_secrets(value):
			if isinstance(value, str):
				matches = secret_pattern.findall(value)
				for placeholder in matches:
					if placeholder in sensitive_data:
						value = value.replace(f'<secret>{placeholder}</secret>', sensitive_data[placeholder])
				return value
			elif isinstance(value, dict):
				return {k: replace_secrets(v) for k, v in value.items()}
			elif isinstance(value, list):
				return [replace_secrets(v) for v in value]
			return value

		# Only write back fields where a secret was actually substituted. Writing the
		# model_dump()'d value back for EVERY field coerces non-string field types
		# (a nested BaseModel becomes a dict, a datetime a str, etc.) even when no
		# secret was present.
		for key, value in params.model_dump().items():
			new_value = replace_secrets(value)
			if new_value != value:
				params.__dict__[key] = new_value
		return params

	def create_action_model(self) -> Type[ActionModel]:
		"""Creates a Pydantic model from registered actions"""
		fields = {
			name: (
				Optional[action.param_model],
				Field(default=None, description=action.description),
			)
			for name, action in self.registry.actions.items()
		}
		
		# Group actions by tool for better logging
		actions_by_tool = {}
		# Create thread-safe snapshot to avoid iterator corruption
		for name, action in list(self.registry.actions.items()):
			tool = action.tool or "default"
			if tool not in actions_by_tool:
				actions_by_tool[tool] = []
			actions_by_tool[tool].append(name)

		# Log actions by tool
		for tool, actions in actions_by_tool.items():
			self.logger.debug(f"Tool '{tool}' has {len(actions)} actions: {', '.join(actions[:5])}..." if len(actions) > 5 else f"Tool '{tool}' has {len(actions)} actions: {', '.join(actions)}")
		
		# Prepare registered functions for telemetry
		registered_functions = [
			RegisteredFunction(
				name=name, 
				params=action.param_model.model_json_schema(),
				service=action.tool or "default"  # SINGULAR: tool field only, no fallback
			)
			for name, action in self.registry.actions.items()
		]

		self.telemetry.capture(
			ControllerRegisteredFunctionsTelemetryEvent(
				registered_functions=registered_functions
			),
			session_id=self.session_id
		)

		return create_model('ActionModel', __base__=ActionModel, **fields)  # type:ignore

	def get_prompt_description(self) -> str:
		"""Get a description of all actions for the prompt"""
		return self.registry.get_prompt_description()

	def get_action_schema(self, action_name: str) -> Dict[str, Any]:
		"""Get the expected schema for a specific action

		Args:
			action_name: Name of the action

		Returns:
			Dictionary containing field information for the action
		"""
		if action_name not in self.registry.actions:
			return {}

		action = self.registry.actions[action_name]
		schema = {}

		if action.param_model:
			try:
				for field_name, field_info in action.param_model.model_fields.items():
					schema[field_name] = {
						'required': field_info.is_required(),
						'type': str(field_info.annotation)
					}
			except Exception:
				pass

		return schema

	def get_actions(self) -> Dict[str, Any]:
		"""Get all registered actions with their details.

		This is a stable public API method to access actions without
		directly accessing internal registry structures.

		Returns:
			Dictionary mapping action names to their details
		"""
		if hasattr(self.registry, 'actions'):
			return dict(self.registry.actions)
		return {}

	def get_action_names(self) -> list[str]:
		"""Get list of all registered action names.

		Returns:
			List of action names
		"""
		if hasattr(self.registry, 'actions'):
			return list(self.registry.actions.keys())
		return []

	def get_action_count(self) -> int:
		"""Get the number of registered actions.

		Returns:
			Number of registered actions
		"""
		if hasattr(self.registry, 'actions'):
			return len(self.registry.actions)
		return 0

	def get_action_details(self, action_name: str) -> Optional[Any]:
		"""Get details for a specific action.

		Args:
			action_name: Name of the action to get details for

		Returns:
			Action details if found, None otherwise
		"""
		if hasattr(self.registry, 'actions'):
			return self.registry.actions.get(action_name)
		return None

	def has_action(self, action_name: str) -> bool:
		"""Check if an action is registered.

		Args:
			action_name: Name of the action to check

		Returns:
			True if action is registered, False otherwise
		"""
		if hasattr(self.registry, 'actions'):
			return action_name in self.registry.actions
		return False

	def get_actions_by_service(self, service_name: str) -> Dict[str, Any]:
		"""Get all actions for a specific tool/service.

		Args:
			service_name: Name of the tool/service

		Returns:
			Dictionary of actions belonging to the tool
		"""
		if not hasattr(self.registry, 'actions'):
			return {}

		service_actions = {}
		# Create thread-safe snapshot to avoid iterator corruption
		for name, action in list(self.registry.actions.items()):
			# Check both 'tool' and '_tool' attributes (tool is the primary attribute)
			if ((hasattr(action, 'tool') and action.tool == service_name) or
			    (hasattr(action, '_tool') and action._tool == service_name)):
				service_actions[name] = action
		return service_actions

	def get_tools(self) -> list[str]:
		"""Get list of all tools that have registered actions.

		Returns:
			List of unique tool names
		"""
		if not hasattr(self.registry, 'actions'):
			return []

		tools = set()
		for action in self.registry.actions.values():
			if hasattr(action, 'tool') and action.tool:
				tools.add(action.tool)
		return list(tools)

	# Keep deprecated alias for backward compatibility
	def _register_default_actions(self) -> None:
		"""Register default actions if available.

		Handles core actions like 'done' which are not tool-specific.
		"""
		# NOTE: The 'done' action is now registered by the Controller
		# which has more sophisticated logic including todo progress checking
		# and quality gate enforcement. This method is kept for future
		# default actions that might be needed at the registry level.
		pass

	def extract_tool_actions(self, tool_name: str, tool: Any) -> Dict[str, Callable]:
		"""Extract all available actions from a tool.

		This is the public API for extracting actions. It returns action functions
		with metadata attached (_tool, _service, _description, _param_model).
		The returned actions are NOT yet registered - call wrap_function() to register.

		Args:
			tool_name: Name of the tool
			tool: Tool instance

		Returns:
			Dictionary of action_name -> action_function
		"""
		actions = {}
		
		# Method 1: Check for get_actions method - preferred approach
		if hasattr(tool, 'get_actions') and callable(getattr(tool, 'get_actions')):
			try:
				self.logger.debug(f"Attempting to get actions from {tool_name}.get_actions()")
				service_actions = tool.get_actions()
				if service_actions and isinstance(service_actions, dict):
					# Normalize actions - some services return {name: function} and others 
					# return {name: {function: func, description: desc, param_model: model}}
					for name, action in service_actions.items():
						if callable(action):
							# Simple action is directly callable
							actions[name] = action
							# Fix missing _tool attribute - check if it's a bound method first
							try:
								if not hasattr(action, '_tool') or action._tool is None:
									self._set_tool_attribution(action, tool_name)
								# Keep _service for backward compat
								if not hasattr(action, '_service') or action._service is None:
									action._service = tool_name
								self.logger.debug(f"Applied tool name '{tool_name}' to action '{name}'")
							except AttributeError:
								# Can't set attributes on bound methods, skip
								self.logger.debug(f"Skipping attribute setting for bound method '{name}' from tool '{tool_name}'")
						elif isinstance(action, dict) and 'function' in action:
							# Complex action has additional metadata
							if callable(action['function']):
								# Store the function with its metadata as attributes
								func = action['function']
								
								# Add description as attribute if available
								if 'description' in action:
									func._description = action['description']
									
								# Add param_model as attribute if available    
								if 'param_model' in action:
									func._param_model = action['param_model']
									
								# Add tool name as attribute
								self._set_tool_attribution(func, tool_name)
								# Keep _service for backward compat
								func._service = tool_name
								
								# Store normalized action
								actions[name] = func
							else:
								self.logger.warning(f"Action '{name}' from tool '{tool_name}' has non-callable function")
						else:
							self.logger.warning(f"Action '{name}' from tool '{tool_name}' has invalid format")
			except Exception as e:
				self.logger.warning(f"❌ Error calling get_actions() on tool '{tool_name}': {e}")
				# Try the register_decorated_actions method as a fallback
				if hasattr(tool, 'register_decorated_actions') and callable(tool.register_decorated_actions):
					try:
						self.logger.debug(f"Attempting to fix tool '{tool_name}' with register_decorated_actions")
						tool.register_decorated_actions()
						# Try get_actions again
						tool_actions = tool.get_actions()
						if tool_actions and isinstance(tool_actions, dict):
							for name, action in tool_actions.items():
								if callable(action):
									actions[name] = action
									if not hasattr(action, '_tool') and not hasattr(action, '_service'):
										self._set_tool_attribution(action, tool_name)
								elif isinstance(action, dict) and 'function' in action:
									func = action['function']
									self._set_tool_attribution(func, tool_name)
									actions[name] = func
					except Exception as e2:
						self.logger.warning(f"Failed to use register_decorated_actions fallback for '{tool_name}': {e2}")
		
		# Method 2: Check for actions attribute - fallback for legacy
		if hasattr(tool, 'actions') and isinstance(tool.actions, dict):
			for name, action in tool.actions.items():
				if callable(action):
					# Set required metadata if missing
					if not hasattr(action, '_tool'):
						self._set_tool_attribution(action, tool_name)
					# Keep _service for backward compat
					if not hasattr(action, '_service'):
						action._service = tool_name
					if not hasattr(action, '_description'):
						action._description = f"Execute {name} from {tool_name} tool"
					if not hasattr(action, '_param_model'):
						try:
							action._param_model = self._create_param_model(action)
						except Exception:
							continue  # Skip if can't create param model
							
					actions[name] = action
				else:
					self.logger.warning(f"Action '{name}' from tool '{tool_name}' is not callable")
		
		# Method 3: Look for methods explicitly decorated with @action
		# STRICT: Only accept methods with explicit action_info AND param_model
		if not actions:
			for attr_name in dir(tool):
				# Skip private methods, properties and special methods
				if attr_name.startswith('_') or attr_name in ('logger', 'config', 'container', 'status'):
					continue

				attr = getattr(tool, attr_name)

				# STRICT: Only accept if explicitly marked as action with both action_info and param_model
				if callable(attr) and not isinstance(attr, property) and hasattr(attr, 'action_info'):
					action_info = attr.action_info

					# STRICT: Require both description and param_model in action_info
					if 'description' not in action_info:
						self.logger.debug(f"Skipping {attr_name}: action_info missing description")
						continue

					if 'param_model' not in action_info:
						self.logger.debug(f"Skipping {attr_name}: action_info missing param_model")
						continue

					# Add required metadata
					self._set_tool_attribution(attr, tool_name)
					attr._service = tool_name  # Keep for backward compat
					attr._description = action_info['description']
					attr._param_model = action_info['param_model']

					actions[attr_name] = attr
		
		# Log found actions
		if actions:
			self.logger.debug(f"Found {len(actions)} actions for service '{tool_name}': {', '.join(list(actions.keys())[:5])}{'...' if len(actions) > 5 else ''}")
		else:
			self.logger.debug(f"No actions found for service '{tool_name}'")
			
		return actions
		
	def wrap_function(self, name: str, function: Callable, description: str, tool: Optional[str] = None, param_model: Optional[Type[BaseModel]] = None) -> None:
		"""Register a function as an action with the registry.

		Args:
			name: Name of the function
			function: The function to register
			description: Description of what the function does
			tool: Optional tool name to attribute the action to
			param_model: Optional parameter model to use instead of creating one
		"""
		# Skip registration if action is in exclude_actions (defensive check)
		if self.exclude_actions and name in self.exclude_actions:
			self.logger.debug(f"Skipping excluded action: {name}")
			return

		# Check for collision with existing action
		if name in self.registry.actions:
			existing = self.registry.actions[name]
			existing_tool = existing.tool if hasattr(existing, 'tool') else 'core'
			new_tool = tool if tool else 'core'
			if existing_tool != new_tool:
				# Make cross-tool collisions a hard error to enforce proper namespacing
				error_msg = (
					f"Action name collision: '{name}' already registered by tool '{existing_tool}', "
					f"cannot override with tool '{new_tool}'. Use namespaced action names (e.g., '{new_tool}_{name}')"
				)
				self.logger.error(error_msg)
				if self.enforce_execution_context:
					# In strict mode, raise an error
					raise ValueError(error_msg)
				else:
					# In legacy mode, skip registration with warning
					self.logger.warning(f"Skipping registration of '{name}' from tool '{new_tool}' due to collision")
					return
			else:
				# Same tool re-registering - log at INFO level for visibility
				# This can indicate unintentional duplicate registrations
				self.logger.info(
					f"Re-registering action '{name}' for tool '{new_tool}' "
					f"(previous registration will be replaced)"
				)
				# Continue to update the registration
		
		try:
			# Skip trying to set attributes on bound methods
			import inspect
			if tool and not inspect.ismethod(function):
				try:
					if not hasattr(function, '_tool'):
						self._set_tool_attribution(function, tool)
				except AttributeError:
					# Can't set attribute on this type of callable, skip
					pass
			
			# Handle param_model with controlled auto-generation
			if param_model is not None:
				# Use the provided param_model
				pass
			elif hasattr(function, '_param_model') and function._param_model is not None:
				param_model = function._param_model
			else:
				# Auto-generate param_model with warning
				self.logger.warning(
					f"Action '{name}' missing param_model - auto-generating from function signature. "
					f"Consider providing explicit param_model for better type safety."
				)
				try:
					param_model = self._create_param_model(function)
					# Cache on function for future use
					if not inspect.ismethod(function):
						try:
							function._param_model = param_model
						except AttributeError:
							pass  # Can't cache on this type
				except Exception as e:
					self.logger.error(f"Failed to auto-generate param_model for '{name}': {e}")
					return  # Fail if auto-generation fails

			# Validate param_model is a proper Pydantic BaseModel subclass
			from pydantic import BaseModel
			if param_model is None or not isinstance(param_model, type) or not issubclass(param_model, BaseModel):
				self.logger.error(f"Invalid param_model for action '{name}': must be a subclass of BaseModel")
				return  # Fail fast if param_model is invalid
			
			# Wrap sync functions to make them async
			if not iscoroutinefunction(function):
				async def async_wrapper(*args, **kwargs):
					return await asyncio.to_thread(function, *args, **kwargs)
				
				# Copy the signature and other metadata
				async_wrapper.__signature__ = signature(function)
				async_wrapper.__name__ = function.__name__ if hasattr(function, '__name__') else name
				async_wrapper.__doc__ = function.__doc__ if hasattr(function, '__doc__') and function.__doc__ else description
				
				if hasattr(function, '__annotations__'):
					async_wrapper.__annotations__ = function.__annotations__ 
					
				# Copy important attributes for action registration
				if hasattr(function, '_description'):
					async_wrapper._description = function._description
				else:
					async_wrapper._description = description
					
				if hasattr(function, '_param_model'):
					async_wrapper._param_model = function._param_model
				else:
					async_wrapper._param_model = param_model
					
				if hasattr(function, '_tool'):
					async_wrapper._tool = function._tool
				elif tool:
					async_wrapper._tool = tool
					
				wrapped_func = async_wrapper
			else:
				wrapped_func = function
				
			# Set missing attributes on the function (skip for method objects)
			import inspect
			if not inspect.ismethod(wrapped_func):
				try:
					if not hasattr(wrapped_func, '_description'):
						wrapped_func._description = description
					if not hasattr(wrapped_func, '_param_model'):
						wrapped_func._param_model = param_model
					if not hasattr(wrapped_func, '_tool'):
						self._set_tool_attribution(wrapped_func, tool)
				except AttributeError:
					# Can't set attributes on this callable, continue without them
					pass
			
			# Create the action registration
			action = RegisteredAction(
				name=name,
				description=description,
				function=wrapped_func,
				param_model=param_model,
				tool=tool  # The tool this action belongs to (None for core actions)
			)
			
			# Check for collision with existing action
			if name in self.registry.actions:
				existing = self.registry.actions[name]
				existing_tool = existing.tool if hasattr(existing, 'tool') else 'core'
				new_tool = tool if tool else 'core'

				if existing_tool != new_tool:
					# Different tools trying to register same action name - this is a collision
					raise ValueError(
						f"Action name collision: '{name}' already registered by '{existing_tool}', "
						f"cannot register for '{new_tool}'. Use namespaced names to avoid collisions."
					)
				# H5: same tool re-registering — actually REPLACE (the first collision
				# check already logged "will be replaced"). The old code returned here,
				# silently discarding the rebuilt action and leaving the stale closure
				# bound to the previous tool instance. Bust the schema cache so the
				# replacement is re-emitted.
				self._provider_schema_cache.clear()

			# Register with the registry
			self.registry.actions[name] = action

			# Log action registration - only if not already logged
			log_key = f"{tool}:{name}" if tool else name
			if log_key not in self._action_registration_logged:
				self._action_registration_logged.add(log_key)
				if tool:
					self.logger.debug(f"Registered action '{name}' from tool '{tool}'")
				else:
					self.logger.debug(f"Registered action '{name}'")
		except Exception as e:
			self.logger.warning(f"Failed to register action '{name}': {str(e)}", exc_info=True)

	# NOTE: fix_missing_action_descriptions() removed per upgrade instructions
	# Metadata repairs should require explicit @action/metadata; no auto-repair scans


	def get_all_actions_for_provider(self, provider: str) -> List[Dict[str, Any]]:
		"""Generate provider-specific schemas for ALL actions (core + tools)

		This is what the LLM actually uses - both core actions and tool actions.

		Args:
			provider: The LLM provider name (openai, anthropic, gemini, etc.)

		Returns:
			List of all action schemas in the provider's expected format
		"""
		# Memoization: if the registered-action set and exclusions are unchanged,
		# return the previously generated schema list for this provider. The cache
		# key embeds the full action-name set so any registration/removal busts it.
		cache_key = (
			provider,
			frozenset(self.registry.actions.keys()),
			frozenset(self.exclude_actions),
		)
		cached = self._provider_schema_cache.get(cache_key)
		if cached is not None:
			return cached

		# Get all non-excluded actions, deduplicated by object identity
		# This is necessary because create_alias() adds the same RegisteredAction
		# under multiple keys (the original name and the alias name).
		# Without deduplication, we'd generate duplicate schemas.
		seen_actions = set()  # Track by object id
		all_actions = []

		for name, action in self.registry.actions.items():
			if name in self.exclude_actions:
				continue
			# Deduplicate by RegisteredAction object identity
			action_id = id(action)
			if action_id in seen_actions:
				self.logger.debug(f"Skipping alias '{name}' -> '{action.name}' (already included)")
				continue
			seen_actions.add(action_id)
			all_actions.append(action)

		if not all_actions:
			self.logger.debug(f"No actions available for provider {provider}")
			return []

		# Get the appropriate schema generator
		generator = get_schema_generator(provider)

		# Generate and return all action schemas
		actions = generator.generate_tools_list(all_actions)

		# UP-10 2.5: sanitize the emitted tools list for broad backend compatibility
		# (nullable-union collapse, $ref-sibling strip, top-level combinator strip,
		# bare-string/array-type normalization). Runs BEFORE the cache write and is
		# composed downstream of DROP_TOOL (sanitize first => fewer tools dropped).
		# Gated TOOL_SCHEMA_SANITIZE (default on; =false restores pre-port bytes).
		if os.getenv("TOOL_SCHEMA_SANITIZE", "true").lower() != "false":
			try:
				from tools.controller.registry.schema_sanitizer import sanitize_emitted_tools
				actions = sanitize_emitted_tools(actions, provider)
			except Exception as e:
				self.logger.warning(f"schema sanitizer skipped (fail-open): {e}")

		# Log breakdown of action types
		core_count = sum(1 for a in all_actions if a.tool is None)
		tool_count = sum(1 for a in all_actions if a.tool is not None)
		alias_count = len(self.registry.actions) - len(all_actions)
		self.logger.debug(
			f"Generated {len(actions)} action schemas for provider {provider} "
			f"({core_count} core, {tool_count} tool, {alias_count} aliases deduplicated)"
		)

		self._provider_schema_cache[cache_key] = actions
		return actions

	# Convenience API methods for accessing registry
	def get_action(self, name: str) -> Optional[RegisteredAction]:
		"""Get a registered action by name.

		Args:
			name: The action name

		Returns:
			RegisteredAction if found, None otherwise
		"""
		return self.registry.actions.get(name)

	def create_alias(self, alias_name: str, target_name: str) -> bool:
		"""Create a thread-safe alias for an existing action.

		Args:
			alias_name: New name for the alias
			target_name: Existing action name to alias

		Returns:
			True if alias created, False if target doesn't exist
		"""
		with self._registry_lock:
			# Check if target exists
			if target_name not in self.registry.actions:
				return False

			# Create alias by copying the RegisteredAction reference
			self.registry.actions[alias_name] = self.registry.actions[target_name]
			return True

	def list_action_names(self) -> List[str]:
		"""Get list of all registered action names.

		Returns:
			List of action names
		"""
		return list(self.registry.actions.keys())

	def list_actions(self) -> List[str]:
		"""Get list of all registered action names.

		Alias for list_action_names().
		"""
		return self.list_action_names()

	def get_actions_by_tool(self, tool: str) -> List[RegisteredAction]:
		"""Get all actions registered for a specific tool.

		Args:
			tool: The tool name

		Returns:
			List of RegisteredAction instances for this tool
		"""
		return [
			action for action in self.registry.actions.values()
			if action.tool == tool
		]

	def supports_native_tools(self, provider: str) -> bool:
		"""Check if a provider supports native tool/function calling

		Args:
			provider: The LLM provider name

		Returns:
			True if the provider has native tool support
		"""
		provider_lower = provider.lower()

		# Providers with native tool support
		# DeepSeek V3+ supports OpenAI-compatible function calling
		# Docs: https://api-docs.deepseek.com/guides/function_calling
		# OpenRouter models support native tool calling via OpenAI-compatible API
		native_providers = ["openai", "anthropic", "gemini", "google", "deepseek", "openrouter", "nvidia"]

		return any(p in provider_lower for p in native_providers)

	def tool_call_to_action(self, tool_name: str, args: Dict[str, Any]) -> ActionModel:
		"""Convert a single tool call to an ActionModel (simplified single validation path)

		Args:
			tool_name: Name of the tool/action
			args: Arguments for the tool call

		Returns:
			ActionModel instance ready for execution

		Raises:
			ValueError: If the tool is not registered or validation fails
		"""
		# 1. Get action from registry (with fuzzy match)
		action = self._get_action_with_fuzzy_match(tool_name)

		# 2. Validate parameters directly
		try:
			if action.param_model:
				validated_args = action.param_model(**args)
				args = validated_args.model_dump()
		except Exception as validation_error:
			# SINGULAR RESPONSIBILITY: If validation fails, caller did not normalize properly
			# Do NOT hide errors with fallback corrections - fail fast
			self.logger.error(
				f"Validation failed for {tool_name}. "
				f"Tool calls MUST be normalized with normalize_and_correct() before validation. "
				f"Args: {args}, Error: {validation_error}"
			)
			raise ValueError(
				f"Invalid arguments for {tool_name}: {validation_error}. "
				f"Ensure tool calls are normalized with ToolCallBuilder.normalize_and_correct() first."
			)

		# 4. Create ActionModel — key by the RESOLVED registered action name, not the
		# caller's (possibly fuzzy-matched) tool_name. Keying by the unresolved name
		# builds an ActionModel with a key that matches no field, which ActionModel
		# silently drops → an empty no-op action (the fuzzy match would validate args
		# but then execute nothing).
		resolved_name = getattr(action, "name", tool_name) or tool_name
		action_data = {resolved_name: args if args is not None else {}}
		action_model_class = self.create_action_model()
		return action_model_class(**action_data)

	def _get_action_with_fuzzy_match(self, tool_name: str) -> RegisteredAction:
		"""Get action from registry with fuzzy matching fallback."""
		# First try exact match
		if tool_name in self.registry.actions:
			return self.registry.actions[tool_name]

		# Try fuzzy matching - look for actions that end with the tool name
		fuzzy_matches = [
			action_name for action_name in self.registry.actions.keys()
			if action_name.endswith(f"_{tool_name}") or action_name == tool_name
		]

		if fuzzy_matches:
			tool_name = fuzzy_matches[0]
			self.logger.info(f"Fuzzy matched to '{tool_name}'")
			return self.registry.actions[tool_name]

		# Not found - provide helpful error
		available = list(self.registry.actions.keys())[:10]
		available_str = ", ".join(available)
		if len(self.registry.actions) > 10:
			available_str += f", ... ({len(self.registry.actions)} total)"

		similar = [
			name for name in self.registry.actions.keys()
			if tool_name in name or name.split('_')[-1] == tool_name.split('_')[-1]
		]
		error_msg = f"Unknown tool: '{tool_name}'. Available: {available_str}."
		if similar:
			error_msg += f" Similar: {similar[:5]}"

		raise ValueError(error_msg)


	def tool_calls_to_actions(self, tool_calls: List[Dict[str, Any]]) -> List[ActionModel]:
		"""Convert multiple tool calls to ActionModels

		Args:
			tool_calls: List of tool calls, each with 'name' or 'function.name' and 'arguments'

		Returns:
			List of ActionModel instances ready for execution

		Note:
			Failed validations are tracked in self._last_validation_errors as a dict mapping
			tool_call_id -> error_message. Callers can check this to provide error responses.
		"""
		actions = []
		# Track validation errors for caller to handle (keyed by tool_call_id)
		self._last_validation_errors: Dict[str, str] = {}

		for call in tool_calls:
			tool_name = call.get('name', 'unknown')
			tool_call_id = call.get('id')

			try:
				args = call.get('args', {})

				if not tool_name or tool_name == 'unknown':
					error_msg = f"Tool call missing 'name': {call}"
					self.logger.warning(error_msg)
					if tool_call_id:
						self._last_validation_errors[tool_call_id] = error_msg
					continue

				# Ensure args is a dict (should already be from normalization)
				if isinstance(args, str):
					import json
					try:
						args = json.loads(args) if args else {}
					except json.JSONDecodeError:
						error_msg = f"Invalid JSON arguments for {tool_name}: {args}"
						self.logger.warning(error_msg)
						if tool_call_id:
							self._last_validation_errors[tool_call_id] = error_msg
						args = {}

				action = self.tool_call_to_action(tool_name, args)
				if tool_call_id:
					action._tool_call_id = tool_call_id
				actions.append(action)

			except ValueError as e:
				error_str = str(e)

				# FIXED: Differentiate between "not found" and "validation failed" errors
				if "Unknown tool" in error_str or "not found" in error_str.lower():
					self.logger.error(f"❌ Tool '{tool_name}' not found in registry!")
					self.logger.error(f"   Available tools (first 30): {list(self.registry.actions.keys())[:30]}")
					error_msg = f"Tool '{tool_name}' not found. Check available tools with mcp_list_tools."
				else:
					# Validation failed - tool exists but args are wrong
					self.logger.error(f"❌ Validation failed for tool '{tool_name}'")
					self.logger.error(f"   Error: {e}")
					error_msg = f"Validation failed for '{tool_name}': {error_str}"

					# Show expected schema for the tool
					if tool_name in self.registry.actions:
						action_info = self.registry.actions[tool_name]
						if action_info.param_model:
							try:
								schema = action_info.param_model.model_json_schema()
								required = schema.get('required', [])
								props = list(schema.get('properties', {}).keys())
								self.logger.error(f"   Required params: {required}")
								self.logger.error(f"   All params: {props}")
								error_msg += f" Required: {required}. All params: {props}."
							except Exception:
								pass

				# FIXED: Track error for caller instead of appending None
				if tool_call_id:
					self._last_validation_errors[tool_call_id] = error_msg

				self.logger.warning(f"   Skipping action '{tool_name}' due to error")
				continue

			except Exception as e:
				error_msg = f"Failed to convert tool call {tool_name}: {e}"
				self.logger.error(error_msg)
				if tool_call_id:
					self._last_validation_errors[tool_call_id] = error_msg
				continue

		# Log summary of validation errors
		if self._last_validation_errors:
			self.logger.warning(
				f"⚠️ {len(self._last_validation_errors)} tool calls failed validation: "
				f"{list(self._last_validation_errors.keys())}"
			)

		return actions

	def get_last_validation_errors(self) -> Dict[str, str]:
		"""Get validation errors from the last tool_calls_to_actions call.

		Returns:
			Dict mapping tool_call_id -> error_message for failed validations
		"""
		return getattr(self, '_last_validation_errors', {})
