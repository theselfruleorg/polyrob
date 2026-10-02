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
				# 041 phase 2: an APPROVED tenant worker is a profile too. The
				# legacy registry only reads the global code-tree dir, so without
				# this a dispatched worker raised "Profile not found". The same
				# gate as the dispatch refusal: flag ON + approved + this tenant.
				from agents.task.agent.profile_store import get_store, workers_enabled
				if workers_enabled():
					_orch = agent_config.get('orchestrator')
					_uid = getattr(_orch, 'user_id', None)
					profile = get_store().get_approved(profile_id, user_id=_uid)
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
			limits_config = {**profile.limits, **profile_overrides.get('limits', {})}
			if prompt_config.get('prompt_type') or prompt_config.get('prompt_source'):
				# A4: build with the session's REAL context — native tools, the
				# loaded tool ids, the model/provider (family notes), the sub-agent
				# contract and the profile's own failure limit. The old call passed
				# only action_description + max_actions_per_step, so a sub-agent got
				# the JSON response format, every tool advertised, no family note and
				# the top-level "done is never delivered" contract.
				ctx = ProfileManager._prompt_context(agent_config, profile, limits_config)
				action_descriptions = ctx.pop('action_description')
				prompt_params = prompt_config.get('prompt_params') or {}
				if not isinstance(prompt_params, dict):
					prompt_params = {}
				prompt_params = {k: v for k, v in prompt_params.items()
				                 if k not in ('action_description', 'max_actions_per_step')}
				system_message = resolve_system_prompt(
					prompt_type=prompt_config.get('prompt_type', 'system'),
					prompt_source=prompt_config.get('prompt_source', 'builtin'),
					prompt_params={**ctx, **prompt_params},
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

	@staticmethod
	def _prompt_context(agent_config: Dict[str, Any], profile: Any,
	                    limits_config: Dict[str, Any]) -> Dict[str, Any]:
		"""The SystemPrompt kwargs a profile-built prompt needs (A4). Fail-open:
		each field degrades to the SystemPrompt default on any error."""
		# The least-privilege child controller when one was injected, else the
		# shared parent controller the agent will use.
		controller = (agent_config.get('controller') or agent_config.get('injected_controller')
		              or getattr(agent_config.get('orchestrator'), 'controller', None))
		llm = agent_config.get('llm')
		profile_llm = getattr(profile, 'llm', None) or {}

		model_name = ""
		for attr in ('model_name', 'model'):
			value = getattr(llm, attr, None) if llm is not None else None
			if isinstance(value, str) and value:
				model_name = value
				break
		if not model_name:
			model_name = str(profile_llm.get('model') or "")

		provider = ""
		if llm is not None:
			try:
				from modules.llm.usage_extract import resolve_serving_provider
				provider = str(resolve_serving_provider(llm, model_name) or "")
			except Exception:
				provider = ""
		if not provider:
			provider = str(profile_llm.get('provider') or "")

		use_native = agent_config.get('use_native_tools')
		use_native = True if use_native is None else bool(use_native)
		if use_native and controller is not None and provider:
			try:
				use_native = bool(controller.supports_native_tools(provider))
			except Exception:
				pass

		tool_ids = None
		action_description = ""
		if controller is not None:
			try:
				tool_ids = list(controller.list_tools())
			except Exception:
				tool_ids = None
			try:
				action_description = (controller.get_prompt_action_index() if use_native
				                      else controller.registry.get_prompt_description())
			except Exception as e:
				logger.warning(f"Could not get action descriptions from controller: {e}")

		max_failures = limits_config.get('max_failures') or agent_config.get('max_failures')
		ctx: Dict[str, Any] = {
			'action_description': action_description,
			'use_native_tools': use_native,
			'model_name': model_name,
			'provider': provider,
			'sub_agent': bool(agent_config.get('is_sub_agent')),
			'include_vision': bool(agent_config.get('use_vision', True)),
		}
		if tool_ids is not None:
			ctx['tool_ids'] = tool_ids
		if isinstance(max_failures, int) and max_failures > 0:
			ctx['max_failures'] = max_failures
		return ctx
