"""Dependency container for bot components."""

import logging
from typing import Dict, Any, Optional, List, Type, Set, Tuple
from core.config import BotConfig
from core.exceptions import ContainerError
from core.logging import get_component_logger
from enum import Enum
from dataclasses import dataclass

class ServiceScope(Enum):
    """Service lifetime scopes."""
    SINGLETON = "singleton"    # One instance for entire app
    SCOPED = "scoped"         # One instance per scope (e.g. conversation)
    TRANSIENT = "transient"   # New instance each time

@dataclass
class ServiceRegistration:
    """Service registration metadata."""
    instance: Any
    scope: ServiceScope
    is_core: bool = False
    is_optional: bool = False
    is_healthy: bool = True
    error_message: Optional[str] = None

class DependencyContainer:
    _instance = None
    
    @classmethod
    def get_instance(cls, config: BotConfig = None) -> 'DependencyContainer':
        """Singleton accessor with lazy initialization.

        Process-wide singleton: the FIRST config wins. A later call that passes a
        DIFFERENT config gets the EXISTING instance — reconfiguring a live container
        mid-run is unsafe, so the new config is deliberately NOT applied. P1
        finalization: warn instead of SILENTLY discarding, so a caller that expected
        its config to take effect can see why it didn't.
        """
        if cls._instance is None:
            if not config:
                raise ValueError("Configuration required for first initialization")
            cls._instance = cls(config)
        elif config is not None and config is not getattr(cls._instance, "_config", None):
            get_component_logger("DependencyContainer").warning(
                "get_instance() called with a config but a singleton already exists — "
                "the passed config is IGNORED (first config wins). Reuse the existing "
                "instance, or reset the singleton explicitly if a fresh container is intended."
            )
        return cls._instance

    def __init__(self, config: BotConfig):
        if self._instance is not None:
            raise RuntimeError("Use get_instance() instead of direct instantiation")
        """Initialize container with config."""
        # Initialize logger first
        self.logger = get_component_logger("DependencyContainer")
        
        if not config or not hasattr(config, 'is_initialized'):
            self.logger.error("Invalid configuration provided")
            raise ValueError("Valid configuration required")
            
        if not config.is_initialized:
            self.logger.error("Configuration not initialized")
            raise ValueError("Configuration must be initialized")
            
        self._config = config
        self._services: Dict[str, ServiceRegistration] = {}
        self._managers: Dict[str, Any] = {}
        self._agents: Dict[str, Any] = {}
        self._initialized_services: Set[str] = set()
        
        # Register config as core service
        self.register_core_service(
            'config',
            config,
            scope=ServiceScope.SINGLETON
        )

    @property
    def config(self) -> BotConfig:
        """Get configuration."""
        return self._config

    @property
    def services(self) -> Dict[str, Any]:
        """Get all registered services."""
        return self._services

    @property
    def managers(self) -> Dict[str, Any]:
        """Get all registered managers."""
        return self._managers

    @property
    def agents(self) -> Dict[str, Any]:
        """Get all registered agents."""
        return self._agents


    def register_core_service(
        self, 
        name: str, 
        service: Any,
        scope: ServiceScope = ServiceScope.SINGLETON
    ) -> None:
        """Register a core service."""
        if name in self._services:
            raise ValueError(f"Service {name} already registered")
            
        self._services[name] = ServiceRegistration(
            instance=service,
            scope=scope,
            is_core=True,
            is_optional=False  # Explicitly set core services as non-optional
        )
        self._initialized_services.add(name)  # Mark as initialized
        self.logger.debug(f"Registered core service: {name}")

    def register_service(
        self,
        name: str,
        instance: Any,
        is_optional: bool = False,
        scope: ServiceScope = ServiceScope.SINGLETON
    ) -> None:
        """Register service."""
        if name in self._services:
            # Optionally replace existing service
            self.logger.warning(f"⚠️ Service {name} already registered, replacing")
            self._services[name] = ServiceRegistration(
                instance=instance,
                scope=scope,
                is_optional=is_optional
            )
        else:
            self._services[name] = ServiceRegistration(
                instance=instance,
                scope=scope,
                is_optional=is_optional
            )
            self._initialized_services.add(name)
            # Improve logging visibility for service registration
            service_type = type(instance).__name__
            optional_flag = "optional" if is_optional else "required"
            self.logger.info(f"🔧 Registered {optional_flag} service: {name} ({service_type})")

    def unregister_service(self, name: str) -> bool:
        """Unregister a service by name.
        
        Args:
            name: Service name to unregister
            
        Returns:
            True if the service was found and unregistered, False otherwise
        """
        if name in self._services:
            del self._services[name]
            if name in self._initialized_services:
                self._initialized_services.remove(name)
            self.logger.debug(f"Unregistered service: {name}")
            return True
        return False

    def get_service(self, name: str) -> Optional[Any]:
        """Get a service by name."""
        registration = self._services.get(name)
        if not registration:
            return None
        return registration.instance

    def has_service(self, name: str) -> bool:
        """Check if service exists."""
        return name in self._services

    async def cleanup(self) -> None:
        """Clean up all services."""
        # Clean up in reverse dependency order
            
        for agent in self._agents.values():
            await agent.cleanup()
            
        for manager in self._managers.values():
            await manager.cleanup()
            
        for service in self._services.values():
            await service.instance.cleanup()
        
        self.logger.info("Container cleanup completed")

    def register_manager(self, name: str, manager: Any) -> None:
        """Register a manager instance."""
        from core.initialization import MANAGER_METADATA

        metadata = MANAGER_METADATA.get(name, {})
        is_optional = metadata.get('optional', False)
        
        if name in self._managers and not is_optional:
            self.logger.warning(f"Overwriting existing manager: {name}")
        
        self._managers[name] = manager
        # Register service with optional flag
        self.register_service(
            name=name,
            instance=manager,
            scope=ServiceScope.SINGLETON,
            is_optional=is_optional
        )

    def register_agent(self, name: str, agent: Any) -> None:
        """Register an agent."""
        self._agents[name] = agent
        # Also register as a service
        self.register_service(name, agent)
        self.logger.debug(f"Agent registered: {name}")

    def get_agent(self, name: str) -> Optional[Any]:
        """Get an agent by name."""
        return self._agents.get(name)


