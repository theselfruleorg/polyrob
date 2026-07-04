"""
Profile management for Task agents.

This module handles loading and applying agent profiles from the profile registry.
Profiles contain configuration for prompts, LLM settings, tools, and limits.
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class ProfileManager:
	"""Manages agent profile loading and application."""

	@staticmethod
	def load_and_apply(
		profile_id: str,
		agent_config: Dict[str, Any],
		profile_overrides: Optional[Dict[str, Any]] = None
	) -> Dict[str, Any]:
		"""Load profile and return updated agent configuration.

		Args:
			profile_id: Profile identifier to load
			agent_config: Current agent configuration dictionary (from __init__ locals())
			profile_overrides: Optional overrides for profile settings

		Returns:
			Updated configuration dictionary with profile settings applied

		Raises:
			AgentError: If profile not found or loading fails
		"""
		try:
			from agents.task.agent.profile_registry import get_profile
			from agents.task.agent.prompts import resolve_system_prompt
			from core.exceptions import AgentError

			# Load profile
			profile = get_profile(profile_id)
			if not profile:
				error_msg = f"Profile '{profile_id}' not found - cannot initialize agent"
				logger.error(error_msg)
				raise AgentError(error_msg)

			# Merge profile with overrides
			profile_overrides = profile_overrides or {}

			# Create updated config dict (copy to avoid mutation)
			updated_config = {**agent_config}

			# Apply prompt configuration
			prompt_config = {**profile.prompt, **profile_overrides.get('prompt', {})}
			if prompt_config.get('prompt_type') or prompt_config.get('prompt_source'):
				# Get action descriptions from controller if available
				action_descriptions = ""
				if 'controller' in agent_config and agent_config['controller']:
					try:
						action_descriptions = agent_config['controller'].registry.get_prompt_description()
					except Exception as e:
						logger.warning(f"Could not get action descriptions from controller: {e}")

				# Use the resolver to get the system message with action_description
				system_message = resolve_system_prompt(
					prompt_type=prompt_config.get('prompt_type', 'system'),
					prompt_source=prompt_config.get('prompt_source', 'builtin'),
					prompt_params=prompt_config.get('prompt_params', {}),
					task=agent_config['task'],
					action_description=action_descriptions,
					max_actions_per_step=agent_config.get('max_actions_per_step', 10)
				)
				# Store system message for later use
				updated_config['_profile_system_message'] = system_message

			# Apply LLM configuration (if not already provided)
			if not agent_config.get('llm'):
				llm_config = {**profile.llm, **profile_overrides.get('llm', {})}
				updated_config['_profile_llm_config'] = llm_config

			# Apply tools configuration
			tools_config = {**profile.tools, **profile_overrides.get('tools', {})}
			if tools_config.get('enabled_actions'):
				updated_config['_enabled_actions'] = tools_config['enabled_actions']
			if tools_config.get('tool_calling_method'):
				updated_config['tool_calling_method'] = tools_config['tool_calling_method']

			# Apply limits configuration
			limits_config = {**profile.limits, **profile_overrides.get('limits', {})}
			if 'max_steps' in limits_config:
				updated_config['_profile_max_steps'] = limits_config['max_steps']
			if 'max_actions_per_step' in limits_config:
				updated_config['max_actions_per_step'] = limits_config['max_actions_per_step']
			if 'max_input_tokens' in limits_config and limits_config['max_input_tokens']:
				updated_config['max_input_tokens'] = limits_config['max_input_tokens']
			if 'max_failures' in limits_config:
				updated_config['max_failures'] = limits_config['max_failures']

			logger.info(f"Applied profile '{profile_id}' with {len(profile_overrides)} overrides")
			return updated_config

		except Exception as e:
			logger.error(f"Failed to apply profile '{profile_id}': {e}", exc_info=True)
			raise
