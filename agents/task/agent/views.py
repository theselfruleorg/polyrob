from __future__ import annotations

import json
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Union

from core.exceptions import RateLimitError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator

from tools.browser.views import BrowserState, BrowserStateHistory, TabInfo
from tools.controller.registry.views import ActionModel
from tools.controller.types import ActionResult  # Canonical location for ActionResult
from tools.dom.history_tree_processor.service import (
	DOMElementNode,
	DOMHistoryElement,
	HistoryTreeProcessor,
)
from tools.dom.views import SelectorMap
from agents.task.path import pm


@dataclass
class AgentStepInfo:
	step_number: int
	max_steps: int
	
	__slots__ = ('step_number', 'max_steps')


class AgentBrain(BaseModel):
	"""Current state of the agent with enhanced tracking"""

	page_summary: str = ""  # Optional with default - not requested in native tools JSON format
	evaluation_previous_goal: str
	memory: str
	next_goal: str
	reasoning: Optional[str] = ""
	thinking: Optional[str] = None  # OPTIMIZATION: For chain-of-thought models (Task 5 - Nov 14, 2025)

	# Enhanced tracking fields for intelligent completion
	acceptance_criteria: Optional[List[str]] = None  # What must be done to complete the task
	working_memory: Optional[Dict[str, Any]] = None  # Key-value pairs to track
	confidence_level: Optional[float] = None  # 0-1 confidence in current approach
	blockers: Optional[List[str]] = None  # Current blockers or issues
	# Minimal hierarchical reasoning (keep lightweight)
	phase: Optional[str] = None  # discovery | collection | processing | documentation
	macro_goal: Optional[str] = None  # short description of overarching goal
	subgoal: Optional[str] = None  # immediate subgoal derived from macro
	immediate_step: Optional[str] = None  # the next concrete step
	
	model_config = ConfigDict(slots=True)


class AgentOutput(BaseModel):
	"""Output model for agent"""

	model_config = ConfigDict(arbitrary_types_allowed=True, extra='forbid', slots=True)

	current_state: AgentBrain
	# CRITICAL: Accept dicts OR ActionModel instances (including MCP subclasses)
	# Don't force validation to ActionModel - it breaks MCP tool dicts
	action: list[Union[Dict[str, Any], ActionModel]]

	@model_validator(mode='before')
	@classmethod
	def handle_thinking_wrapper(cls, values: Dict[str, Any]) -> Dict[str, Any]:
		"""Handle LLM responses that wrap content in '_thinking' field.

		Some LLMs return: {"_thinking": "{...}", "action": []}
		We need to extract the actual content from _thinking and merge it with the outer structure.

		This fixes the native tool calling validation failure where the LLM puts current_state
		inside a _thinking string instead of at the top level.

		Also filters out None values from action list to prevent validation failures.
		"""
		import logging
		logger = logging.getLogger(__name__)

		if isinstance(values, dict):
			# Handle _thinking wrapper
			if '_thinking' in values:
				import json
				try:
					# Parse the JSON string inside _thinking
					thinking_content = values['_thinking']
					if isinstance(thinking_content, str):
						parsed = json.loads(thinking_content)
						# Merge parsed content into values
						# Keep action from outer level if present, use inner if not
						if 'action' not in values and 'action' in parsed:
							values['action'] = parsed['action']
						# Always prefer inner current_state if present
						if 'current_state' in parsed:
							values['current_state'] = parsed['current_state']
						# Remove the _thinking wrapper
						del values['_thinking']
						logger.debug("Unwrapped _thinking field successfully")
				except (json.JSONDecodeError, TypeError) as e:
					logger.warning(f"Failed to parse _thinking field: {e}")
					# Keep original values if parsing fails

			# FIXED: Filter out None values from action list
			# This prevents validation failures when tool_calls_to_actions skips invalid actions
			if 'action' in values and isinstance(values['action'], list):
				original_len = len(values['action'])
				values['action'] = [a for a in values['action'] if a is not None]
				filtered_count = original_len - len(values['action'])
				if filtered_count > 0:
					logger.warning(
						f"⚠️ Filtered out {filtered_count} None values from action list "
						f"(likely from failed tool call validations)"
					)

		return values

	@staticmethod
	def type_with_custom_actions(custom_actions: Type[ActionModel]) -> Type['AgentOutput']:
		"""Extend actions with custom actions"""
		model_ = create_model(
			'AgentOutput',
			__base__=AgentOutput,
			action=(list[custom_actions], Field(...)),  # Properly annotated field with no default
			__module__=AgentOutput.__module__,
		)
		model_.__doc__ = "AgentOutput model"
		
		# FIXED: Explicitly set model_config to ensure extra='forbid' is inherited
		# This prevents the "additionalProperties is required to be supplied and to be false" error
		model_.model_config = ConfigDict(arbitrary_types_allowed=True, extra='forbid', slots=True)
		
		# CRITICAL FIX: Ensure the dynamically created model inherits our schema method
		model_.get_openai_schema = classmethod(AgentOutput.get_openai_schema.__func__)
		
		return model_
		
	@classmethod
	def get_openai_schema(cls) -> Dict[str, Any]:
		"""Get OpenAI-compatible schema with explicit additionalProperties control."""
		try:
			# Use centralized schema fix utility
			from agents.task.utils import fix_openai_schema
			schema = cls.model_json_schema()
			return fix_openai_schema(schema)
			
		except Exception as e:
			# ULTRA-SAFE FALLBACK: If all else fails, return a minimal working schema
			return {
				"type": "object",
				"properties": {
					"current_state": {
						"type": "object",
					"properties": {
						"page_summary": {"type": "string"},
						"evaluation_previous_goal": {"type": "string"}, 
						"memory": {"type": "string"},
						"next_goal": {"type": "string"},
						"reasoning": {"type": "string"}
					},
					"required": ["page_summary", "evaluation_previous_goal", "memory", "next_goal"],
						"additionalProperties": False
					},
					"action": {
						"type": "array",
						"items": {
							"type": "object",
							"additionalProperties": False
						}
					}
				},
				"required": ["current_state", "action"], 
				"additionalProperties": False
			}


class AgentHistory(BaseModel):
	"""History item for agent actions"""

	model_output: AgentOutput | None
	result: list[ActionResult]
	state: BrowserStateHistory

	model_config = ConfigDict(arbitrary_types_allowed=True, protected_namespaces=(), slots=True)

	@staticmethod
	def get_interacted_element(model_output: AgentOutput, selector_map: SelectorMap) -> list[DOMHistoryElement | None]:
		"""Get the elements interacted with from model output.
		
		Args:
			model_output: The output from the model containing actions
			selector_map: Mapping of element indices to DOM elements
			
		Returns:
			List of DOM history elements that were interacted with, aligned with actions
		"""
		elements = []
		for action in model_output.action:
			index = action.get_index()
			if index is not None and index in selector_map:
				el: DOMElementNode = selector_map[index]
				elements.append(HistoryTreeProcessor.convert_dom_element_to_history_element(el))
			else:
				elements.append(None)
		return elements

	def model_dump(self, **kwargs) -> Dict[str, Any]:
		"""Custom serialization handling circular references"""

		# Handle action serialization
		model_output_dump = None
		if self.model_output:
			action_dump = [action.model_dump(exclude_none=True) for action in self.model_output.action]
			model_output_dump = {
				'current_state': self.model_output.current_state.model_dump(),
				'action': action_dump,  # This preserves the actual action data
			}

		return {
			'model_output': model_output_dump,
			'result': [r.model_dump(exclude_none=True) for r in self.result],
			'state': self.state.to_dict(),
		}


class AgentHistoryList(BaseModel):
	"""List of agent history items"""

	history: list[AgentHistory]
	
	model_config = ConfigDict(slots=True)

	def __str__(self) -> str:
		"""Representation of the AgentHistoryList object"""
		return f'AgentHistoryList(all_results={self.action_results()}, all_model_outputs={self.model_actions()})'

	def __repr__(self) -> str:
		"""Representation of the AgentHistoryList object"""
		return self.__str__()

	def save_to_file(self, filepath: str | Path, session_id: Optional[str] = None) -> None:
		"""Save history to JSON file with proper serialization.

		This method ensures proper path handling for session-specific files.

		Args:
			filepath: Path to save history file
			session_id: Optional session ID for path normalization
		"""
		try:
			# If session_id explicitly provided, use it for normalization
			if session_id:
				clean_id = pm().clean_session_id(session_id)
				filepath = pm().normalize_path(str(filepath), clean_id)
			else:
				# Try to extract session ID from path for backward compatibility
				import re
				filepath_str = str(filepath)
				uuid_pattern = r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
				uuid_match = re.search(uuid_pattern, filepath_str)

				if uuid_match:
					# Path contains a session ID, normalize it
					session_id = uuid_match.group(1)
					clean_id = pm().clean_session_id(session_id)
					filepath = pm().normalize_path(filepath_str, clean_id)
				
			# Create parent directory if needed
			Path(filepath).parent.mkdir(parents=True, exist_ok=True)
			
			# Serialize and save
			data = self.model_dump()
			with open(filepath, 'w', encoding='utf-8') as f:
				json.dump(data, f, indent=2)
		except Exception as e:
			raise e

	def model_dump(self, **kwargs) -> Dict[str, Any]:
		"""Custom serialization that properly uses AgentHistory's model_dump"""
		return {
			'history': [h.model_dump(**kwargs) for h in self.history],
		}

	@classmethod
	def load_from_file(cls, filepath: str | Path, output_model: Type[AgentOutput], session_id: Optional[str] = None) -> 'AgentHistoryList':
		"""Load history from JSON file.

		This method handles normalizing paths and validating session data.

		Args:
			filepath: Path to the history file
			output_model: Model type for agent output validation
			session_id: Optional session ID for path normalization

		Returns:
			Loaded AgentHistoryList instance
		"""
		# Normalize path with explicit session_id if provided
		try:
			if session_id:
				clean_id = pm().clean_session_id(session_id)
				filepath = pm().normalize_path(str(filepath), clean_id)
			else:
				# Try to extract session ID from path for backward compatibility
				import re

				filepath_str = str(filepath)
				uuid_pattern = r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
				uuid_match = re.search(uuid_pattern, filepath_str)

				if uuid_match:
					# Path contains a session ID, normalize it
					session_id = uuid_match.group(1)
					clean_id = pm().clean_session_id(session_id)
					filepath = pm().normalize_path(filepath_str, clean_id)
		except ImportError:
			# Continue with original path if path module not available
			pass
			
		with open(filepath, 'r', encoding='utf-8') as f:
			data = json.load(f)
			
		# Validate and enrich data
		if 'history' not in data:
			raise ValueError(f"Invalid history file format: {filepath}")
			
		# Process each history entry
		for h in data['history']:
			# Handle model output if present
			if h['model_output']:
				if isinstance(h['model_output'], dict):
					try:
						h['model_output'] = output_model.model_validate(h['model_output'])
					except Exception:
						# If validation fails, set to None rather than breaking
						h['model_output'] = None
				else:
					h['model_output'] = None
					
			# Ensure state has interacted_element field
			if 'state' in h and 'interacted_element' not in h['state']:
				h['state']['interacted_element'] = None
				
		history = cls.model_validate(data)
		return history

	def last_action(self) -> None | dict:
		"""Last action in history"""
		if self.history and self.history[-1].model_output and self.history[-1].model_output.action:
			actions = self.history[-1].model_output.action
			if actions and len(actions) > 0:
				return actions[-1].model_dump(exclude_none=True)
		return None

	def errors(self) -> list[str]:
		"""Get all errors from history"""
		errors = []
		for h in self.history:
			errors.extend([r.error for r in h.result if r.error])
		return errors

	def final_result(self) -> None | str:
		"""Final result from history"""
		if self.history and self.history[-1].result[-1].extracted_content:
			return self.history[-1].result[-1].extracted_content
		return None

	def is_done(self) -> bool:
		"""Check if the agent is done"""
		if self.history and len(self.history[-1].result) > 0 and self.history[-1].result[-1].is_done:
			return self.history[-1].result[-1].is_done
		return False

	def has_errors(self) -> bool:
		"""Check if the agent has any errors"""
		return len(self.errors()) > 0

	def urls(self) -> list[str]:
		"""Get all unique URLs from history"""
		return [h.state.url for h in self.history if h.state.url]

	def screenshots(self) -> list[str]:
		"""Get all screenshots from history"""
		return [h.state.screenshot for h in self.history if h.state.screenshot]

	def action_names(self) -> list[str]:
		"""Get all action names from history"""
		action_names = []
		for action in self.model_actions():
			actions = list(action.keys())
			if actions:
				action_names.append(actions[0])
		return action_names

	def model_thoughts(self) -> list[AgentBrain]:
		"""Get all thoughts from history"""
		return [h.model_output.current_state for h in self.history if h.model_output]

	def model_outputs(self) -> list[AgentOutput]:
		"""Get all model outputs from history"""
		return [h.model_output for h in self.history if h.model_output]

	def model_actions(self) -> list[dict]:
		"""Get all actions from history"""
		outputs = []

		for h in self.history:
			if h.model_output and h.model_output.action:
				# FIX: Safe iteration with proper length handling
				actions = h.model_output.action
				interacted_elements = h.state.interacted_element if h.state.interacted_element else []
				
				for i, action in enumerate(actions):
					if action is None:
						continue
						
					output = action.model_dump(exclude_none=True)
					# Safely get interacted element if available
					if i < len(interacted_elements):
						output['interacted_element'] = interacted_elements[i]
					else:
						output['interacted_element'] = None
					outputs.append(output)
		return outputs

	def action_results(self) -> list[ActionResult]:
		"""Get all results from history"""
		results = []
		for h in self.history:
			results.extend([r for r in h.result if r])
		return results

	def extracted_content(self) -> list[str]:
		"""Get all extracted content from history"""
		content = []
		for h in self.history:
			content.extend([r.extracted_content for r in h.result if r.extracted_content])
		return content

	def model_actions_filtered(self, include: list[str] = []) -> list[dict]:
		"""Get all model actions from history as JSON"""
		outputs = self.model_actions()
		result = []
		for o in outputs:
			for i in include:
				if i == list(o.keys())[0]:
					result.append(o)
		return result


class AgentError:
	"""Container for agent error handling"""

	VALIDATION_ERROR = 'Invalid model output format. Please follow the correct schema.'
	RATE_LIMIT_ERROR = 'Rate limit reached. Waiting before retry.'
	NO_VALID_ACTION = 'No valid action found'

	@staticmethod
	def format_error(error: Exception, include_trace: bool = False) -> str:
		"""Format error message based on error type and optionally include trace"""
		if isinstance(error, ValidationError):
			return f'{AgentError.VALIDATION_ERROR}\nDetails: {str(error)}'
		if isinstance(error, RateLimitError):
			return AgentError.RATE_LIMIT_ERROR
		if include_trace:
			return f'{str(error)}\nStacktrace:\n{traceback.format_exc()}'
		return f'{str(error)}'
