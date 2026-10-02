"""Prompt data anchors.

H8: UnifiedPromptComposer + PromptPackage were dead code and were retired in the
prompt-stack consolidation. 2026-09-29 (review F16): SystemPromptManager and
BasePromptManager were retired too — nothing read their prompts, and the manager
rewrote data/prompts/system_prompts.json on every shutdown. The live prompt path
is the task agent's SystemPrompt builder (agents/task/agent/prompts.py).
"""

from pathlib import Path

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
