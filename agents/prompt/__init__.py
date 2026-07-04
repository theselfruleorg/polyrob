"""Prompt management and composition system.

This module provides a comprehensive prompt management system including:
- Base prompt management functionality
- JSON-based prompt storage and retrieval
- Unified prompt composition for different LLM types
- System prompt management with character integration
"""

from .base_prompt import BasePromptManager
from .system import SystemPromptManager
from pathlib import Path

# H8: UnifiedPromptComposer + PromptPackage (agents/prompt/prompt_composer.py) were
# dead code — exported but never instantiated anywhere. Retired in the prompt-stack
# consolidation. The live prompt path is SystemPromptManager / the task agent's
# SystemPrompt builder.

__all__ = [
    'BasePromptManager',
    'SystemPromptManager'
]

# Version info
from core.version import __version__  # noqa: F401  (project version SSOT)

# Default configuration
DEFAULT_CONFIG = {
    'max_history_messages': 5,
    'max_knowledge_tokens': 1500,
    'min_relevance_score': 0.6,
    'default_response_type': 'factual'
}

# Response types supported by the system
RESPONSE_TYPES = {
    'factual': 'Precise and factual responses with source citations',
    'creative': 'Creative responses while maintaining factual accuracy',
    'analysis': 'Detailed analysis with structured format',
    'conversational': 'Natural, engaging dialogue while maintaining accuracy'
}

# Agent types supported by the system
AGENT_TYPES = {
    'chat_agent': 'Primary conversational agent',
    'task_agent': 'Task automation agent'
}

# Model types supported by the system
MODEL_TYPES = {
    'anthropic': 'Anthropic Claude models',
    'openai': 'OpenAI GPT models',
    'default': 'Default text completion models'
}

# Default prompts directory — anchored to the install/repo root (this file is
# agents/prompt/__init__.py -> parents[2]), NOT the process CWD.
DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "data" / "prompts"
