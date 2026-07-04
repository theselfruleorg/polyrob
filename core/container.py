"""Dependency container for bot components."""

import logging
from typing import Dict, Any, Optional, List, Type, Set, Tuple
from core.config import BotConfig
from core.exceptions import ContainerError, DependencyError
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
        """Singleton accessor with lazy initialization"""
        if cls._instance is None:
            if not config:
                raise ValueError("Configuration required for first initialization")
            cls._instance = cls(config)
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
        self._initialized_components = set()  # Track initialized component groups
        
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
            
            # >>> NEW: If user_profile_manager is registered, link it to permissions
            if name == 'user_profile_manager' and 'permissions' in self._services:
                try:
                    permissions_service = self._services['permissions'].instance
                    if hasattr(permissions_service, 'set_user_profile_manager'):
                        permissions_service.set_user_profile_manager(instance)
                        self.logger.info("Linked user_profile_manager with permissions service")
                except Exception as link_error:
                    self.logger.error(f"Failed to link user_profile_manager with permissions: {link_error}")

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

    def get_required_services(self, names: List[str]) -> Dict[str, Any]:
        """Get multiple required services."""
        services = {}
        missing = []
        
        for name in names:
            service = self.get_service(name)
            if service:
                services[name] = service
            else:
                missing.append(name)
                
        if missing:
            raise DependencyError(f"Missing required services: {', '.join(missing)}")
            
        return services

    def get_optional_services(self, names: List[str]) -> Dict[str, Any]:
        """Get multiple optional services."""
        return {
            name: service 
            for name in names
            if (service := self.get_service(name)) is not None
        }

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

    def get_manager(self, name: str) -> Optional[Any]:
        """Get a manager by name."""
        return self._managers.get(name)

    def get_managers(self) -> Dict[str, Any]:
        """Get all registered managers."""
        return self._managers.copy()

    def register_agent(self, name: str, agent: Any) -> None:
        """Register an agent."""
        self._agents[name] = agent
        # Also register as a service
        self.register_service(name, agent)
        self.logger.debug(f"Agent registered: {name}")

    def get_agent(self, name: str) -> Optional[Any]:
        """Get an agent by name."""
        return self._agents.get(name)


    def get_service_description(self, name: str) -> str:
        """Get service description."""
        descriptions = {
            'database_manager': 'Database management service',
            'memory_manager': 'Memory management service',
            'cache_manager': 'Cache management service',
            'permissions': 'Permissions service',
            'private_conversation_manager': 'Private conversation manager',
            'chat_agent': 'Chat conversation agent',
            'task_agent': 'Task agent',
            'system_prompt_manager': 'System prompt management service',
            'knowledge_base': 'Knowledge base service',
            'llm_client': 'LLM client service',
            'anthropic_client': 'Anthropic Claude LLM service',
            'openai_client': 'OpenAI GPT LLM service',
            'deepseek_client': 'DeepSeek LLM service',
            'gemini_client': 'Google Gemini LLM service',
            'embedding_model': 'Text embedding model service',
            'vector_storage': 'Vector storage service',
            'browser': 'Web browser automation service',
        }
        return descriptions.get(name, f"Unknown service: {name}")

    async def is_super_admin(self, user_id: int) -> bool:
        """Check if user is super admin.

        Args:
            user_id: User ID to check

        Returns:
            bool: True if user is super admin

        Note:
            This method is async because the underlying Permissions service methods are async.
        """
        permissions = self.get_service('permissions')
        if permissions:
            return await permissions.is_super_admin(user_id)

        # No fallback available - permissions service required for super admin checks
        return False

    async def is_admin(self, user_id: int) -> bool:
        """Check if user is admin.

        Args:
            user_id: User ID to check

        Returns:
            bool: True if user is admin

        Note:
            This method is async because the underlying Permissions service methods are async.
        """
        permissions = self.get_service('permissions')
        if permissions:
            return await permissions.is_admin(user_id)

        # Fallback to checking against config if permissions service isn't available
        if hasattr(self.config, 'admin_ids'):
            return user_id in self.config.admin_ids

        return False

    async def check_permission(self, user_id: int, permission: str) -> bool:
        """Check if user has specific permission.

        Args:
            user_id: User ID to check
            permission: Permission to check

        Returns:
            bool: True if user has permission

        Note:
            This method is async because the underlying Permissions service methods are async.
        """
        permissions = self.get_service('permissions')
        if permissions:
            return await permissions.check_permission(user_id, permission)

        # Default to false if permissions service isn't available
        return False

    def mark_component_group_initialized(self, group_name: str) -> None:
        """Mark a component group as initialized."""
        self._initialized_components.add(group_name)

    def is_component_group_initialized(self, group_name: str) -> bool:
        """Check if a component group is initialized."""
        return group_name in self._initialized_components 

    def mark_service_optional(self, name: str) -> None:
        """Mark a service as optional in the registry."""
        if name in self._services:
            self._services[name].is_optional = True 

    async def initialize_services(self) -> None:
        """Explicitly initialize core services."""
        for name, registration in self._services.items():
            if registration.is_core and name not in self._initialized_services:
                try:
                    if hasattr(registration.instance, 'initialize'):
                        await registration.instance.initialize()
                    self._initialized_services.add(name)
                    self.logger.info(f"✨ ✓ {name} service initialized")
                except Exception as e:
                    self.logger.error(f"Service {name} initialization failed: {e}")
                    raise

    def register_core_services(self):
        """Register core services required for bot operation."""
        try:
            # Register document processor service
            from tools.filesystem import FileSystem
            filesystem = FileSystem("filesystem", self.config, self)
            self.register_service("filesystem", filesystem)
            
            self.logger.info("Core services registered")
        except Exception as e:
            self.logger.error(f"Error registering core services: {e}")
            raise ContainerError(f"Failed to register core services: {e}")

    def initialize_components(self):
        """Initialize all registered components with container reference"""
        for name, registration in self._services.items():
            component = registration.instance
            if not hasattr(component, 'name'):
                component.name = name  # Ensure name exists
            component.container = self
            self.logger.debug(f"Initialized component: {name}")

    def register_llm_clients(self, config: BotConfig):
        """Register LLM clients based on configuration."""
        try:
            # Import here to avoid circular dependency
            from modules.llm import create_llm_client
            
            # Register OpenAI client for auto agent
            openai_client = create_llm_client('openai', config, self)
            if openai_client:
                self.register_service('openai_client', openai_client)
                self.logger.info("✓ OpenAI client registered")
                
            # Register Anthropic client for other agents
            anthropic_client = create_llm_client('anthropic', config, self)
            if anthropic_client:
                self.register_service('anthropic_client', anthropic_client)
                self.logger.info("✓ Anthropic client registered")
                
            # Register DeepSeek client if configured
            deepseek_client = create_llm_client('deepseek', config, self)
            if deepseek_client:
                self.register_service('deepseek_client', deepseek_client)
                self.logger.info(f"✓ DeepSeek client registered with model {deepseek_client.model_type}")
            else:
                self.logger.debug("DeepSeek client not configured (missing API key)")
                
            # Register Gemini client if configured
            gemini_client = create_llm_client('gemini', config, self)
            if gemini_client:
                self.register_service('gemini_client', gemini_client)
                self.logger.info(f"✓ Gemini client registered with model {gemini_client.model_type}")
            else:
                self.logger.debug("Gemini client not configured (missing API key)")
                
        except Exception as e:
            self.logger.error(f"Error registering LLM clients: {e}")


    def get_service_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all registered services."""
        return {
            name: {
                'status': getattr(reg.instance, 'status', 'unknown').value if hasattr(getattr(reg.instance, 'status', None), 'value') else getattr(reg.instance, 'status', 'unknown'),
                'healthy': reg.is_healthy,
                'error': reg.error_message,
                'optional': reg.is_optional
            }
            for name, reg in self._services.items()
        }

    async def create_permissions_service(self) -> None:
        """Create permissions service."""
        try:
            self.logger.info("Creating permissions service")
            
            # Use dynamic import to prevent circular import
            from core.permissions import Permissions
            
            # Create permissions service
            permissions = Permissions(
                name='permissions',
                config=self.config
            )
            
            # Initialize permissions
            await permissions.initialize()
            
            # Register in container 
            self.register_service('permissions', permissions)
            
            # Note: We don't need to set user_profile_manager here
            # This happens in register_service when user_profile_manager
            # is registered (for consistency)
            
            self.logger.info("Permissions service created")
        except Exception as e:
            self.logger.error(f"Failed to create permissions service: {e}")
            raise ContainerError(f"Failed to create permissions service: {e}")