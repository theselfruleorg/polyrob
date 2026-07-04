"""Core modules package.

Lazy package (PEP 562): the LLM clients pull their provider SDKs and the managers pull the
memory/LLM stack at module load. Importing any `modules.*` leaf (e.g. modules.llm.profiles
on the CLI / server boot) must NOT eager-load them, so the heavy re-exports and the
class-capturing metadata tables resolve on first attribute access. The light, SDK-free
names (BaseModule + the memory dataclasses) stay eager to preserve the historical
circular-import pre-load. See docs/plans/2026-06-26-runtime-architecture-finalization-FUSION.md (P0c).
"""

from typing import TYPE_CHECKING

# Light, eager (no SDK; pre-loaded to break a historical circular import).
from .base_module import BaseModule
from .memory.models import (
    Message,
    MessageRole,
    ConversationContext,
    UserProfile,
)

# Heavy re-exports resolved lazily by __getattr__.
_LAZY = {
    "DatabaseManager": ".database.database_manager",
    "MemoryManager": ".memory.memory_manager",
    "CacheManager": ".memory.cache_manager",
    "LLMClient": ".llm.llm_client",
    "AnthropicClient": ".llm.anthropic_client",
    "OpenAIClient": ".llm.openai_client",
    "DeepSeekClient": ".llm.deepseek_client",
    "GeminiClient": ".llm.gemini_client",
    "create_llm_client": ".llm.llm_client_registry",
    "AVAILABLE_MODELS": ".llm.llm_client_registry",
}

if TYPE_CHECKING:  # static analysis / IDEs only — no runtime import
    from .database.database_manager import DatabaseManager
    from .memory.memory_manager import MemoryManager
    from .memory.cache_manager import CacheManager
    from .llm.llm_client import LLMClient
    from .llm.anthropic_client import AnthropicClient
    from .llm.openai_client import OpenAIClient
    from .llm.deepseek_client import DeepSeekClient
    from .llm.gemini_client import GeminiClient
    from .llm.llm_client_registry import create_llm_client, AVAILABLE_MODELS


def _build_module_metadata():
    """Module init metadata. Lazily imports the manager/client classes it captures."""
    from .database.database_manager import DatabaseManager
    from .memory.memory_manager import MemoryManager
    from .memory.cache_manager import CacheManager
    from .llm.llm_client import LLMClient
    return {
        'database_manager': {
            'class': DatabaseManager,
            'description': 'Database management system',
            'required': True,
            'dependencies': {'required': [], 'optional': []},
            'init_order': 1,
        },
        'memory_manager': {
            'class': MemoryManager,
            'description': 'Memory and context management',
            'required': True,
            'dependencies': {'required': ['database_manager'], 'optional': ['llm_client']},
            'init_order': 2,
        },
        'cache_manager': {
            'class': CacheManager,
            'description': 'Caching system',
            'required': False,
            'dependencies': {'required': ['memory_manager'], 'optional': []},
            'init_order': 3,
        },
        'llm_client': {
            'class': LLMClient,
            'description': 'Language model client',
            'required': False,
            'dependencies': {'required': [], 'optional': ['cache_manager']},
            'init_order': 4,
        },
    }


def __getattr__(name: str):
    """PEP 562 lazy resolution; caches into globals() so it fires once per name."""
    if name in _LAZY:
        import importlib
        value = getattr(importlib.import_module(_LAZY[name], __name__), name)
        globals()[name] = value
        return value
    if name == "MODULE_METADATA":
        value = _build_module_metadata()
        globals()[name] = value
        return value
    if name == "MODULE_INIT_ORDER":
        metadata = globals().get("MODULE_METADATA") or _build_module_metadata()
        globals()["MODULE_METADATA"] = metadata
        value = sorted(metadata.items(), key=lambda x: x[1]['init_order'])
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY) | {"MODULE_METADATA", "MODULE_INIT_ORDER"})


__all__ = [
    'BaseModule',
    'DatabaseManager',
    'MemoryManager',
    'CacheManager',
    'LLMClient',
    'AnthropicClient',
    'OpenAIClient',
    'DeepSeekClient',
    'GeminiClient',
    'create_llm_client',
    'AVAILABLE_MODELS',
    'MODULE_METADATA',
    'MODULE_INIT_ORDER',
]
