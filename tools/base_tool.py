"""Base tool class for all bot tools.

This module provides the base class for all tools in the system.
Tools are registered with the Controller and their actions are exposed
to LLM agents for execution.

Attribute naming conventions:
- `_tool` or `_service`: The tool name this action belongs to (legacy: _service)
- `_description`: Action description for LLM
- `_param_model`: Pydantic model for action parameters
- `action_info`: DEPRECATED - legacy dict, prefer direct attributes
"""

import logging
import asyncio
from typing import Dict, Any, Optional, List, Type
from core.config import BotConfig
from core.exceptions import ConfigurationError, DependencyError, ToolError
from core.base_component import BaseComponent
from core.logging import get_component_logger
from core.container import DependencyContainer
from enum import Enum
from pydantic import BaseModel
from functools import wraps

# Tool-specific exceptions are in tools.exceptions module
# Import from there: from tools.exceptions import ToolSystemError, ActionNotFoundError, etc.

class ToolStatus(Enum):
    """Tool status states."""
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing" 
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    DISABLED = "disabled"  # intentionally off (e.g. missing optional API key) — not a failure

class BaseTool(BaseComponent):
    """Base class for tools."""
    
    def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
        """Initialize tool."""
        super().__init__(name=name, config=config, container=container)
        self._status = ToolStatus.UNINITIALIZED
        self._error_message = None
        self._services = {}
        self.logger.debug(f"Created {self.__class__.__name__} instance")
        self.logger.debug(f"- Name: {name}")
        self.logger.debug(f"- Container: {'Present' if container else 'None'}")
        
    @property
    def services(self) -> Dict[str, Any]:
        """Get tool dependencies."""
        return self._services

    @property
    def required_services(self) -> Dict[str, str]:
        """Get required tool dependencies."""
        return {
            'rate_limit_manager': 'Rate limit management'
        }

    @property
    def optional_services(self) -> Dict[str, str]:
        """Get optional tool dependencies."""
        return {}

    @property
    def required_config(self) -> Dict[str, str]:
        """Get required configuration keys.
        
        Returns:
            Dictionary mapping config key to description
        """
        # Get required config from metadata if available
        try:
            from tools import TOOL_METADATA
            metadata = TOOL_METADATA.get(self.name, {})
            config_keys = metadata.get('requires_config', [])
            return {key: f"Required configuration for {self.name}" for key in config_keys}
        except ImportError:
            return {}
        
    @property
    def status(self) -> ToolStatus:
        """Get service status."""
        return self._status

    @property
    def error_message(self) -> Optional[str]:
        """Get error message if any."""
        return self._error_message
    
    def get_actions(self) -> Dict[str, Dict[str, Any]]:
        """Get available actions for this service.
        
        Returns:
            Dictionary mapping action names to their details
        """
        # Default implementation that can be overridden by services
        actions = {}
        
        # Skip known non-action methods that should not be registered
        known_non_action_methods = {
            'initialize', 'cleanup', 'disable', 'enable', 
            'ensure_initialized', 'get_dependency', 'set_service', 
            'get_service', 'validate_dependencies', 'get_actions',
            'test_connection', 'get_status', 'get_status_report',
            'rate_limit', 'close', 'register_decorated_actions'
        }
        
        # Look for methods with action_info attribute or _description attribute
        for attr_name in dir(self):
            # Skip internal methods and known non-action methods
            if attr_name.startswith('_') or attr_name in known_non_action_methods:
                continue
                
            attr = getattr(self, attr_name)
            if callable(attr) and not isinstance(attr, property):
                # Check for action decorator metadata (_description or action_info)
                decorated_with_action = hasattr(attr, 'action_info') or hasattr(attr, '_description')
                
                if decorated_with_action:
                    # Fix missing _description attribute if needed (solves common issue)
                    if not hasattr(attr, '_description'):
                        # Try to get description from action_info or docstring
                        if hasattr(attr, 'action_info') and 'description' in attr.action_info:
                            attr._description = attr.action_info['description']
                        elif attr.__doc__:
                            # Get first line of docstring
                            attr._description = attr.__doc__.strip().split('\n')[0]
                        else:
                            # Default description
                            attr._description = f"{attr_name} action in {self.name} tool"
                    
                    # Fix missing _param_model attribute if needed
                    if not hasattr(attr, '_param_model'):
                        if hasattr(attr, 'action_info') and 'param_model' in attr.action_info:
                            attr._param_model = attr.action_info['param_model']
                    
                    # Fix missing _service attribute if needed
                    if not hasattr(attr, '_service'):
                        attr._service = self.name
                        
                    # Add to actions dictionary - return decorated function directly
                    # This matches the expected controller format for service registrations
                    actions[attr_name] = attr
        
        return actions
    
    @staticmethod
    def action(description: str, param_model: Optional[Type[BaseModel]] = None):
        """Decorator to mark a method as an action"""
        def decorator(func):
            # Store metadata directly on the function
            func._description = description
            func._param_model = param_model
            
            # Set _service later during registration
            if not hasattr(func, '_service'):
                # Initially set to None, updated during registration
                func._service = None
            
            # Also keep the legacy action_info for backward compatibility
            func.action_info = {
                'description': description,
                'param_model': param_model
            }
            
            # Check if the function is already an async function
            import inspect
            is_async = inspect.iscoroutinefunction(func)

            if is_async:
                # For async functions, just add the metadata
                @wraps(func)
                async def async_wrapper(*args, **kwargs):
                    return await func(*args, **kwargs)

                # Transfer metadata to wrapper
                async_wrapper._description = description
                async_wrapper._param_model = param_model
                async_wrapper._service = getattr(func, '_service', None)
                async_wrapper.action_info = func.action_info
                async_wrapper.__is_action__ = True
                # Preserve the original function signature
                async_wrapper.__signature__ = inspect.signature(func)
                async_wrapper.__annotations__ = func.__annotations__

                return async_wrapper
            else:
                # For sync functions, wrap them in async
                @wraps(func)
                async def async_wrapper(*args, **kwargs):
                    # Make sure function is executed in a way that doesn't block
                    import asyncio
                    result = await asyncio.to_thread(func, *args, **kwargs)
                    return result

                # Transfer metadata to wrapper
                async_wrapper._description = description
                async_wrapper._param_model = param_model
                async_wrapper._service = getattr(func, '_service', None)
                async_wrapper.action_info = func.action_info
                async_wrapper.__is_action__ = True
                # Preserve the original function signature
                async_wrapper.__signature__ = inspect.signature(func)
                async_wrapper.__annotations__ = func.__annotations__

                return async_wrapper
                
        return decorator

    async def initialize(self) -> None:
        """Initialize service with proper status tracking."""
        if self._initialized:
            return

        async with self._lock:
            try:
                self._status = ToolStatus.INITIALIZING
                self.logger.info(f"Initializing {self.name} service...")
                
                # Initialize required services
                for service_name in self.required_services:
                    service = self.container.get_service(service_name)
                    if not service:
                        raise ToolError(f"Required dependency {service_name} not available")
                    self._services[service_name] = service

                # Initialize optional services
                for service_name in self.optional_services:
                    service = self.container.get_service(service_name)
                    if service:
                        self._services[service_name] = service

                # Initialize the service
                await self._initialize()
                
                # Only mark as healthy if _initialize() completed without errors
                self._initialized = True
                self._status = ToolStatus.HEALTHY
                self.logger.info(f"✓ {self.name} service initialized successfully")
                
            except Exception as e:
                self._error_message = str(e)
                self._status = ToolStatus.FAILED
                self.logger.error(f"Failed to initialize {self.name} service: {e}")
                raise ToolError(f"Tool {self.name} failed to initialize: {e}")

    @property
    def rate_limiter(self):
        """Get rate limiter service."""
        return self._services.get('rate_limit_manager')

    @property
    def database(self):
        """Get database service."""
        return self._services.get('database_manager')

    @property
    def cache(self):
        """Get cache service."""
        return self._services.get('cache_manager')

    @property 
    def llm_client(self):
        """Get LLM client service."""
        return self._services.get('llm_client')

    async def _validate_dependencies(self) -> None:
        """Validate service dependencies."""
        await super()._validate_dependencies()
        
        missing = []
        for service_name, description in self.required_services.items():
            if service_name not in self._services:
                missing.append(f"{service_name} ({description})")

        if missing:
            self._enabled = False
            return

        # Log missing optional services
        missing_optional = []
        for service_name, description in self.optional_services.items():
            if service_name not in self._services:
                missing_optional.append(f"{service_name} ({description})")

        if missing_optional:
            self.logger.info(
                f"{self.name} service missing optional services: {', '.join(missing_optional)}"
            )

    @property
    def enabled(self) -> bool:
        """Check if service is enabled."""
        return self._enabled
    
    @enabled.setter
    def enabled(self, value: bool):
        """Set enabled status."""
        self._enabled = value
    
    def _check_enabled(self) -> bool:
        """Check if service is enabled."""
        # Get metadata for this service
        try:
            from tools import TOOL_METADATA
            metadata = TOOL_METADATA.get(self.name, {})
            is_required = metadata.get('required', False)
            dependencies = metadata.get('dependencies', [])
            required_config = metadata.get('requires_config', [])
            
            # Validate configuration
            missing_config = []
            for key in required_config:
                if not hasattr(self.config, key) or not getattr(self.config, key):
                    missing_config.append(f"{key} (Required configuration)")
                    
            if missing_config:
                self.logger.warning(
                    f"{self.name} service missing required configuration: "
                    f"{', '.join(missing_config)}"
                )
                if is_required:
                    return False
                    
            # Validate services
            missing_services = []
            for service_name in dependencies:
                if service_name not in self._services:
                    missing_services.append(f"{service_name} (Required dependency)")
                    
            if missing_services:
                self.logger.warning(
                    f"{self.name} service missing required dependencies: "
                    f"{', '.join(missing_services)}"
                )
                if is_required:
                    return False
                    
            return True
        except ImportError:
            # Fallback to basic validation if metadata not available
            return True

    def set_service(self, name: str, service: Any) -> None:
        """Set a service dependency."""
        self._services[name] = service

    def get_service(self, name: str) -> Optional[Any]:
        """Get a service by name."""
        return self._services.get(name)

    async def cleanup(self) -> None:
        """Clean up service resources."""
        if not self._initialized:
            return

        async with self._lock:
            try:
                await self._cleanup()
                self._initialized = False
                self.logger.info(f"{self.name} cleaned up successfully")
            except Exception as e:
                self.logger.error(f"Failed to clean up {self.name}: {e}")
                raise ToolError(f"Failed to clean up tool {self.name}: {e}")

    async def _initialize(self) -> None:
        """Tool-specific initialization."""
        # Call register_decorated_actions to ensure all @action decorated methods
        # have proper metadata for registration
        self.register_decorated_actions()
        # Subclasses should override this method with their specific initialization

    async def _cleanup(self) -> None:
        """Tool-specific cleanup."""
        pass

    async def disable(self) -> None:
        """Disable the service."""
        if not self.enabled:
            return
            
        try:
            await self.cleanup()
            self.enabled = False
            self.logger.info(f"{self.name} service disabled")
        except Exception as e:
            raise ToolError(f"Failed to disable {self.name} tool: {str(e)}")
            
    async def enable(self) -> None:
        """Enable the service."""
        if self.enabled:
            return
            
        try:
            await self.initialize()
            self.enabled = True
            self.logger.info(f"{self.name} service enabled")
        except Exception as e:
            raise ToolError(f"Failed to enable {self.name} tool: {str(e)}")
            
    async def ensure_initialized(self) -> None:
        """Ensure service is initialized."""
        if not self._initialized:
            await self.initialize()
            
    def __str__(self) -> str:
        """Get string representation."""
        status = "enabled" if self.enabled else "disabled"
        init = "initialized" if self._initialized else "not initialized"
        return f"{self.name} service ({status}, {init})"

    async def get_dependency(self, name: str) -> Any:
        """Get a dependency from tools."""
        if name not in self._services:
            raise DependencyError(f"Dependency {name} not found")
        return self._services[name]

    @property
    def is_critical(self) -> bool:
        """Check if service is critical for system operation."""
        try:
            from tools import TOOL_METADATA
            return TOOL_METADATA.get(self.name, {}).get('is_core', False)
        except ImportError:
            return False

    async def rate_limit(self, key: str) -> None:
        """Apply rate limiting using core rate limiter."""
        if self.rate_limiter:
            await self.rate_limiter.check_rate_limit(f"{self.name}:{key}")

    @property 
    def is_healthy(self) -> bool:
        """Check if service is healthy and usable."""
        return self._status in (ToolStatus.HEALTHY, ToolStatus.DEGRADED)

    def register_decorated_actions(self):
        """Register all methods with proper @action decorators.
        
        This helper method can be called in __init__ to ensure all methods
        decorated with @action are properly registered.
        """
        for attr_name in dir(self):
            if attr_name.startswith('_'):
                continue
                
            attr = getattr(self, attr_name)
            
            # If method has action decorator but missing metadata
            if callable(attr) and (hasattr(attr, 'action_info') or hasattr(attr, '_description')):
                # Set service name if missing
                if not hasattr(attr, '_service'):
                    attr._service = self.name
                
                # Ensure description is set
                if not hasattr(attr, '_description'):
                    if hasattr(attr, 'action_info') and 'description' in attr.action_info:
                        attr._description = attr.action_info['description']
                    else:
                        attr._description = f"Execute {attr_name} from {self.name} tool"
                        
                # Ensure param_model is set if available in action_info
                if not hasattr(attr, '_param_model') and hasattr(attr, 'action_info'):
                    attr._param_model = attr.action_info.get('param_model')