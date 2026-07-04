"""Provider-specific tool schema generators for native tool calling"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type, Tuple
import json
import logging
import os
from pydantic import BaseModel

from tools.controller.registry.views import RegisteredAction
from agents.task.utils import fix_openai_schema, fix_anthropic_schema

logger = logging.getLogger(__name__)


class SchemaValidationError(Exception):
    """Raised when a generated schema fails validation."""
    pass


_VALID_SCHEMA_ERROR_POLICIES = ("DROP_TOOL", "RAISE", "WARN")


def get_schema_error_policy() -> str:
    """Policy for handling a tool whose generated native schema fails validation.

    Env ``TOOL_SCHEMA_ERROR_POLICY`` (default ``DROP_TOOL``):
      - ``DROP_TOOL`` — exclude the offending tool from the emitted list (never
        ship an invalid schema to the provider).
      - ``RAISE``     — propagate ``SchemaValidationError``.
      - ``WARN``      — legacy: log and ship the tool anyway.
    """
    val = os.getenv("TOOL_SCHEMA_ERROR_POLICY", "DROP_TOOL").upper().strip()
    return val if val in _VALID_SCHEMA_ERROR_POLICIES else "DROP_TOOL"


class ToolSchemaGenerator(ABC):
    """Base class for provider-specific tool schema generation"""

    @abstractmethod
    def generate_tool_schema(self, action: RegisteredAction) -> Dict[str, Any]:
        """Generate tool schema for specific provider"""
        pass

    @abstractmethod
    def generate_tools_list(self, actions: List[RegisteredAction]) -> Any:
        """Generate the complete tools list/object for the provider"""
        pass

    def _get_json_schema(self, param_model: Type[BaseModel]) -> Dict[str, Any]:
        """Get JSON schema from Pydantic model, removing unnecessary fields"""
        schema = param_model.model_json_schema()
        # Remove title from properties to reduce token usage
        if 'properties' in schema:
            for prop in schema['properties'].values():
                if isinstance(prop, dict) and 'title' in prop:
                    del prop['title']
        # Remove title from root
        if 'title' in schema:
            del schema['title']
        return schema

    def _validate_schema(self, schema: Dict[str, Any], action_name: str) -> Tuple[bool, List[str]]:
        """Validate a generated schema for common issues.

        Returns:
            Tuple of (is_valid, list of warning/error messages)
        """
        issues = []

        # Check for basic structure
        if not isinstance(schema, dict):
            issues.append(f"Schema for '{action_name}' is not a dictionary")
            return False, issues

        # Check for required fields based on schema type
        if 'function' in schema:
            # OpenAI format
            func = schema.get('function', {})
            if not func.get('name'):
                issues.append(f"OpenAI schema for '{action_name}' missing function name")
            if not func.get('description'):
                issues.append(f"OpenAI schema for '{action_name}' missing description")
            if 'parameters' not in func:
                issues.append(f"OpenAI schema for '{action_name}' missing parameters")
            else:
                params = func.get('parameters', {})
                if params.get('type') != 'object':
                    issues.append(f"OpenAI schema for '{action_name}' parameters should have type 'object'")
        elif 'input_schema' in schema:
            # Anthropic format
            if not schema.get('name'):
                issues.append(f"Anthropic schema for '{action_name}' missing name")
            if not schema.get('description'):
                issues.append(f"Anthropic schema for '{action_name}' missing description")
            input_schema = schema.get('input_schema', {})
            if input_schema.get('type') != 'object':
                issues.append(f"Anthropic schema for '{action_name}' input_schema should have type 'object'")
        elif 'parameters' in schema and 'name' in schema:
            # Gemini format
            if not schema.get('name'):
                issues.append(f"Gemini schema for '{action_name}' missing name")
            if not schema.get('description'):
                issues.append(f"Gemini schema for '{action_name}' missing description")

        # Check for empty properties (debug only - not an error, some actions have no params)
        properties = None
        if 'function' in schema:
            properties = schema.get('function', {}).get('parameters', {}).get('properties')
        elif 'input_schema' in schema:
            properties = schema.get('input_schema', {}).get('properties')
        elif 'parameters' in schema:
            properties = schema.get('parameters', {}).get('properties')

        # Note: Empty properties is not an error - some actions intentionally have no parameters
        # (e.g., get_positions, get_portfolio_value, etc.)
        # Only log at debug level to avoid noise
        if properties is not None and len(properties) == 0:
            logger.debug(f"Schema for '{action_name}' has empty properties (no parameters) - this is expected for parameterless actions")

        is_valid = len([i for i in issues if 'missing' in i.lower()]) == 0
        return is_valid, issues

    def validate_and_log(self, schema: Dict[str, Any], action_name: str) -> Dict[str, Any]:
        """Validate schema and log any issues.

        Args:
            schema: The generated schema
            action_name: Name of the action for logging

        Returns:
            The schema (unchanged)

        Raises:
            SchemaValidationError: If validation fails critically
        """
        is_valid, issues = self._validate_schema(schema, action_name)

        if issues:
            for issue in issues:
                if 'missing' in issue.lower():
                    logger.error(f"Schema validation error: {issue}")
                else:
                    logger.warning(f"Schema validation warning: {issue}")

        if not is_valid:
            raise SchemaValidationError(f"Schema validation failed for '{action_name}': {issues}")

        return schema

    def _apply_schema_error_policy(self, schema: Dict[str, Any], action_name: str) -> Optional[Dict[str, Any]]:
        """Validate ``schema`` and apply ``TOOL_SCHEMA_ERROR_POLICY``.

        Returns the schema if valid (or under WARN policy), ``None`` if the tool
        should be dropped (DROP_TOOL), or raises ``SchemaValidationError`` (RAISE).
        """
        is_valid, issues = self._validate_schema(schema, action_name)
        if is_valid:
            return schema

        policy = get_schema_error_policy()
        msg = f"Schema validation failed for '{action_name}': {issues}"
        if policy == "RAISE":
            raise SchemaValidationError(msg)
        if policy == "WARN":
            logger.warning(f"{msg} (WARN policy: shipping tool anyway)")
            return schema
        # DROP_TOOL (default): never ship an invalid schema to the provider
        logger.warning(f"{msg} (DROP_TOOL policy: excluding this tool from the list)")
        return None


class OpenAISchemaGenerator(ToolSchemaGenerator):
    """OpenAI function calling schema generator"""

    def generate_tool_schema(self, action: RegisteredAction, validate: bool = True) -> Dict[str, Any]:
        """Generate OpenAI function schema"""
        # Get the base schema
        params_schema = self._get_json_schema(action.param_model)

        # Apply OpenAI-specific fixes (additionalProperties: false)
        params_schema = fix_openai_schema(params_schema)

        schema = {
            "type": "function",
            "function": {
                "name": action.name,
                "description": action.description,
                "parameters": params_schema
            }
        }

        # Validate if requested (log warnings but don't fail)
        if validate:
            try:
                self.validate_and_log(schema, action.name)
            except SchemaValidationError as e:
                logger.warning(f"Schema validation issue (continuing anyway): {e}")

        return schema

    def generate_tools_list(self, actions: List[RegisteredAction]) -> List[Dict[str, Any]]:
        """Generate OpenAI tools list with deduplication by name + error policy."""
        seen_names = set()
        tools = []
        for action in actions:
            if action.name in seen_names:
                logger.debug(f"Skipping duplicate tool name: {action.name}")
                continue
            seen_names.add(action.name)
            schema = self._apply_schema_error_policy(
                self.generate_tool_schema(action, validate=False), action.name
            )
            if schema is not None:
                tools.append(schema)
        return tools


class AnthropicSchemaGenerator(ToolSchemaGenerator):
    """Anthropic tool calling schema generator"""

    def generate_tool_schema(self, action: RegisteredAction, validate: bool = True) -> Dict[str, Any]:
        """Generate Anthropic tool schema"""
        # Get the base schema
        params_schema = self._get_json_schema(action.param_model)

        # Apply Anthropic-specific fixes (additionalProperties: false)
        params_schema = fix_anthropic_schema(params_schema)

        schema = {
            "name": action.name,
            "description": action.description,
            "input_schema": params_schema
        }

        # Validate if requested (log warnings but don't fail)
        if validate:
            try:
                self.validate_and_log(schema, action.name)
            except SchemaValidationError as e:
                logger.warning(f"Schema validation issue (continuing anyway): {e}")

        return schema

    def generate_tools_list(self, actions: List[RegisteredAction]) -> List[Dict[str, Any]]:
        """Generate Anthropic tools list with deduplication by name + error policy."""
        seen_names = set()
        tools = []
        for action in actions:
            if action.name in seen_names:
                logger.debug(f"Skipping duplicate tool name: {action.name}")
                continue
            seen_names.add(action.name)
            schema = self._apply_schema_error_policy(
                self.generate_tool_schema(action, validate=False), action.name
            )
            if schema is not None:
                tools.append(schema)
        return tools


class GeminiSchemaGenerator(ToolSchemaGenerator):
    """Google Gemini function calling schema generator"""

    def generate_tool_schema(self, action: RegisteredAction, validate: bool = True) -> Dict[str, Any]:
        """Generate Gemini function schema"""
        base_schema = self._get_json_schema(action.param_model)

        # Gemini expects 'type' and 'properties' at minimum
        parameters = {
            "type": "object",
            "properties": base_schema.get("properties", {}),
        }

        # Only add required if it exists and is non-empty
        if "required" in base_schema and base_schema["required"]:
            parameters["required"] = base_schema["required"]

        schema = {
            "name": action.name,
            "description": action.description,
            "parameters": parameters
        }

        # Validate if requested (log warnings but don't fail)
        if validate:
            try:
                self.validate_and_log(schema, action.name)
            except SchemaValidationError as e:
                logger.warning(f"Schema validation issue (continuing anyway): {e}")

        return schema

    def generate_tools_list(self, actions: List[RegisteredAction]) -> List[Dict[str, Any]]:
        """Generate Gemini tools list with function declarations + error policy."""
        if not actions:
            return []

        declarations = []
        for action in actions:
            schema = self._apply_schema_error_policy(
                self.generate_tool_schema(action, validate=False), action.name
            )
            if schema is not None:
                declarations.append(schema)
        if not declarations:
            return []
        return [{"function_declarations": declarations}]


class JSONFallbackSchemaGenerator(ToolSchemaGenerator):
    """JSON schema generator for providers without native tool support"""

    def generate_tool_schema(self, action: RegisteredAction) -> Dict[str, Any]:
        """Generate JSON schema for action"""
        return {
            "name": action.name,
            "description": action.description,
            "parameters": self._get_json_schema(action.param_model)
        }

    def generate_tools_list(self, actions: List[RegisteredAction]) -> Dict[str, Any]:
        """Generate JSON schema for all actions"""
        # This creates a schema that expects: {"action": [{"action_name": {...params}}]}
        action_properties = {}

        for action in actions:
            action_properties[action.name] = {
                "type": "object",
                "description": action.description,
                "properties": action.param_model.model_json_schema().get("properties", {}),
                "required": action.param_model.model_json_schema().get("required", [])
            }

        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "array",
                    "description": "List of actions to execute",
                    "items": {
                        "type": "object",
                        "properties": action_properties,
                        "additionalProperties": False
                    }
                }
            },
            "required": ["action"]
        }


# Provider registry mapping
# NOTE: DeepSeek V3+ uses OpenAI-compatible function calling API
# NOTE: OpenRouter proxies to many providers and uses OpenAI-compatible format
SCHEMA_GENERATORS = {
    "openai": OpenAISchemaGenerator(),
    "anthropic": AnthropicSchemaGenerator(),
    "gemini": GeminiSchemaGenerator(),
    "google": GeminiSchemaGenerator(),  # Alias
    "deepseek": OpenAISchemaGenerator(),  # DeepSeek V3+ is OpenAI-compatible
    "openrouter": OpenAISchemaGenerator(),  # OpenRouter uses OpenAI-compatible format
    "nvidia": OpenAISchemaGenerator(),  # NVIDIA NIM uses OpenAI-compatible format
    "groq": JSONFallbackSchemaGenerator(),
    "fireworks": JSONFallbackSchemaGenerator(),
    "default": JSONFallbackSchemaGenerator(),
}


# Providers that have intentionally fallen back to the JSON generator are tracked
# so we warn exactly once per unrecognized provider string (avoids log spam).
_warned_fallback_providers: set = set()


def get_schema_generator(provider: str) -> ToolSchemaGenerator:
    """Get the appropriate schema generator for a provider.

    Falls back to the JSON generator for unknown providers — but warns once so a
    misspelled/new provider silently losing native tool-calling is visible
    (see the new-provider checklist: SCHEMA_GENERATORS is the easy-to-miss seam).
    """
    provider_lower = provider.lower()

    # Check for partial matches (e.g., "openai-gpt4" -> "openai")
    for key in SCHEMA_GENERATORS:
        if key != "default" and key in provider_lower:
            return SCHEMA_GENERATORS[key]

    if provider_lower not in _warned_fallback_providers:
        _warned_fallback_providers.add(provider_lower)
        logger.warning(
            f"No native schema generator matched provider '{provider}' — using JSON "
            f"fallback (native tool-calling disabled). If this provider supports native "
            f"tools, add it to SCHEMA_GENERATORS."
        )
    return SCHEMA_GENERATORS["default"]