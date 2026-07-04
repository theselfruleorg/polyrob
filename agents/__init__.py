"""Agents package for bot components.

Lazy package (PEP 562): importing `agents` (or any `agents.*` submodule) must NOT eager-load
the agent/LLM/Telegram stack. Heavy re-exports (BaseAgent, the prompt managers, CharacterManager,
TaskAgent) and the agent-metadata tables load on first attribute access. This keeps leaf imports
like `agents.task.constants` import-light for the CLI and server worker boot.
See docs/plans/2026-06-26-runtime-architecture-finalization-FUSION.md (P0b).
"""

import logging
from typing import TYPE_CHECKING, Optional, Dict, Any

from core.container import DependencyContainer
from core.exceptions import ComponentInitializationError

logger = logging.getLogger(__name__)

# name -> (relative module, attribute) resolved lazily by __getattr__
_LAZY_ATTRS = {
    "BaseAgent": (".base_agent", "BaseAgent"),
    "SystemPromptManager": (".prompt", "SystemPromptManager"),
    "BasePromptManager": (".prompt", "BasePromptManager"),
    "CharacterManager": (".personality.character_manager", "CharacterManager"),
    "TaskAgent": (".task_agent_lite", "TaskAgent"),
}

if TYPE_CHECKING:  # static analysis / IDEs only — no runtime import
    from .base_agent import BaseAgent
    from .prompt import SystemPromptManager, BasePromptManager
    from .personality.character_manager import CharacterManager
    from .task_agent_lite import TaskAgent


def _task_package_available() -> bool:
    """True if the task agent subpackage is importable. Pays the import only when called."""
    try:
        from .task.agent.orchestrator import SessionOrchestrator  # noqa: F401
        return True
    except ImportError:
        return False


_AGENT_OPTIONAL_SERVICES = [
    'filesystem', 'perplexity', 'websearch', 'twitter', 'email', 'cache_manager',
]


def _build_agent_components():
    """Agent init order with deps. Lazily imports TaskAgent (captures the class object)."""
    from .task_agent_lite import TaskAgent
    return [
        ('task_agent', TaskAgent, 'Task agent', True, {
            'required_services': ['llm'],
            'optional_services': list(_AGENT_OPTIONAL_SERVICES),
        })
    ]


def _build_agent_metadata():
    """Agent metadata for consistent naming. Lazily imports TaskAgent."""
    from .task_agent_lite import TaskAgent
    return {
        'task_agent': {
            'class': TaskAgent,
            'description': 'Task agent',
            'is_core': False,
            'optional': True,
            'required_services': ['llm'],
            'optional_services': list(_AGENT_OPTIONAL_SERVICES),
        }
    }


async def initialize_shared_components(container: DependencyContainer) -> None:
    """Initialize shared components used by agents."""
    from .prompt import SystemPromptManager
    from .personality.character_manager import CharacterManager
    try:
        # Initialize required shared components first
        if not container.has_service('system_prompt_manager'):
            logger.debug("Creating system prompt manager")
            system_prompt_manager = SystemPromptManager(
                name='system_prompt_manager',
                config=container.config,
                container=container
            )
            await system_prompt_manager.initialize()
            container.register_service('system_prompt_manager', system_prompt_manager)
            logger.info("✓ System prompt manager initialized")

        # Initialize character manager
        if not container.has_service('character_manager'):
            logger.debug("Creating character manager")
            character_manager = CharacterManager(
                name='character_manager',
                config=container.config,
                container=container
            )
            await character_manager.initialize()
            container.register_service('character_manager', character_manager)
            logger.info("✓ Character manager initialized")

        # Mark shared components as initialized
        container.mark_component_group_initialized('shared_components')

    except Exception as e:
        logger.error(f"Shared component initialization failed: {e}")
        raise


# Names computed lazily (cached into globals() on first access).
_LAZY_COMPUTED = {
    "TASK_PACKAGE_AVAILABLE": _task_package_available,
    "AGENT_COMPONENTS": _build_agent_components,
    "AGENT_METADATA": _build_agent_metadata,
}


def __getattr__(name: str):
    """PEP 562 lazy attribute resolution; caches into globals() so it fires once per name."""
    if name in _LAZY_ATTRS:
        import importlib
        module_path, attr = _LAZY_ATTRS[name]
        value = getattr(importlib.import_module(module_path, __name__), attr)
        globals()[name] = value
        return value
    if name in _LAZY_COMPUTED:
        value = _LAZY_COMPUTED[name]()
        globals()[name] = value
        return value
    if name == "__package_info__":
        return {"task_package_available": _task_package_available()}
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY_ATTRS) | set(_LAZY_COMPUTED) | {"__package_info__"})


__all__ = [
    # Base components
    'BaseAgent',

    # Main agents
    'TaskAgent',

    # Prompt system
    'SystemPromptManager',
    'BasePromptManager',

    # Character system
    'CharacterManager',

    # Utility functions
    'initialize_shared_components',

    # Metadata
    'AGENT_COMPONENTS',
    'AGENT_METADATA',
    'TASK_PACKAGE_AVAILABLE',
]

# Package metadata
from core.version import __version__  # noqa: F401  (project version SSOT)
