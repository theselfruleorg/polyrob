"""
Agent module initialization.

This module exposes the Agent, SessionManager, and other core components.
"""

# Import centralized path manager first to avoid circular dependencies
from agents.task.path import pm

# Import core components
from agents.task.agent.service import Agent
from agents.task.agent.session import SessionManager
from agents.task.agent.orchestrator import SessionOrchestrator
from agents.task.agent.sub_agent_manager import SubAgentManager
from agents.task.path import get_safe_singleton

# Re-export core components
__all__ = [
    "Agent",
    "SessionManager",
    "SessionOrchestrator",
    "SubAgentManager",
]

# Perform initialization tasks
def _initialize():
    """Perform module initialization tasks like session cleanup"""
    try:
        import logging
        logger = logging.getLogger("task.agent_init")
        logger.info("Running agent module initialization")
        
        # Import SessionManager here to avoid circular imports
        from agents.task.agent.session import SessionManager
        
        try:
            # Get the SessionManager instance
            session_manager = get_safe_singleton(SessionManager)()
            logger.info("SessionManager instance initialized")
            
        except Exception as e:
            logger.error(f"SessionManager initialization failed: {e}")
            # We'll continue without a session manager - the application should
            # handle this gracefully by using direct path utilities
            
        # Log completion
        logger.info("Agent module initialization complete")
    except Exception as e:
        # Use basic logging if we can't get the logger
        try:
            import logging
            logging.getLogger("task").warning(f"Agent module initialization failed: {e}")
        except:
            # Last resort - we can't do anything else
            print(f"CRITICAL: Agent module initialization failed: {e}")

# Run initialization only if AUTO_AGENT_INIT environment variable is set.
# SA-08: use the core.env SSOT parser (canonical falsey-set semantics) instead of an
# ad-hoc truthy-set that silently ignored AUTO_AGENT_INIT=on.
from core.env import bool_env as _bool_env
if _bool_env('AUTO_AGENT_INIT', False):
    _initialize()
else:
    # Log that we're skipping initialization 
    try:
        import logging
        logger = logging.getLogger("task.agent_init")
        logger.debug("Skipping agent module initialization (AUTO_AGENT_INIT not set)")
    except:
        pass
