"""Base module class for core functionality modules."""

from typing import Dict, Any, Optional, List, TYPE_CHECKING, Type
import asyncio
import logging
from core.base_component import BaseComponent
from core.config import BotConfig
from core.exceptions import ModuleError, DependencyError
from core.logging import get_component_logger

if TYPE_CHECKING:
    from core.container import DependencyContainer
else:
    # Import for runtime use with forward reference
    from core.container import DependencyContainer

logger = get_component_logger('modules.base_module')

class BaseModule(BaseComponent):
    """Base class for modules."""

    def __init__(self, name: str, config: BotConfig, container: Optional['DependencyContainer'] = None):
        """Initialize base module.
        
        Args:
            name: Module name
            config: Bot configuration
            container: Optional dependency container
        """
        super().__init__(name=name, config=config, container=container)
        self._modules = {}  # For module-specific dependencies
        self.logger = get_component_logger(f"modules.{name}")
        self._lock = asyncio.Lock()
        self._initialized = False
        self.component = self  # Forward reference

    @property
    def required_dependencies(self) -> List[str]:
        """Get module's required dependencies."""
        return list(self.required_modules.keys())

    @property
    def required_modules(self) -> Dict[str, str]:
        """Get required modules with descriptions."""
        return {}

    @property
    def optional_modules(self) -> Dict[str, str]:
        """Get optional modules."""
        return {}

    async def initialize(self) -> None:
        """Initialize module."""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return
                
            try:
                # Register required dependencies from container if available
                if self.container:
                    for module_name in self.required_modules.keys():
                        service = self.container.get_service(module_name)
                        if service:
                            self.set_module(module_name, service)
                
                # Validate dependencies first
                try:
                    self.validate_dependencies()
                except DependencyError as e:
                    self.logger.warning(f"Dependency validation failed: {e}")
                    # Continue initialization - optional modules might be missing
                
                # Call module-specific initialization
                await self._initialize()
                self._initialized = True
                
                # Restore INFO level to ensure visibility of successful initialization
                self.logger.info(f"✨ {self.name} initialized successfully")
            except Exception as e:
                self.logger.error(f"Failed to initialize {self.name}: {e}")
                raise ModuleError(f"Failed to initialize module {self.name}: {e}")

    async def cleanup(self) -> None:
        """Clean up module resources."""
        if not self._initialized:
            return

        async with self._lock:
            try:
                await self._cleanup()
                self._initialized = False
                self.logger.info(f"{self.name} cleaned up successfully")
            except Exception as e:
                self.logger.error(f"Failed to clean up {self.name}: {e}")
                raise ModuleError(f"Failed to clean up module {self.name}: {e}")

    async def _initialize(self) -> None:
        """Module-specific initialization."""
        pass

    async def _cleanup(self) -> None:
        """Module-specific cleanup."""
        pass

    def set_module(self, name: str, module: Any) -> None:
        """Set a required module."""
        self._modules[name] = module

    def get_module(self, name: str) -> Optional[Any]:
        """Get a module by name."""
        return self._modules.get(name)

    def validate_dependencies(self) -> None:
        """Validate module dependencies."""
        missing = []
        for module_name, description in self.required_modules.items():
            # Check both _modules dictionary and container services
            module_exists = module_name in self._modules
            container_has_service = self.container and self.container.has_service(module_name)
            
            if not (module_exists or container_has_service):
                missing.append(f"{module_name} ({description})")

        if missing:
            raise DependencyError(
                f"Module {self.name} missing required modules: {', '.join(missing)}"
            )

        # Log missing optional modules
        missing_optional = []
        for module_name, description in self.optional_modules.items():
            module_exists = module_name in self._modules
            container_has_service = self.container and self.container.has_service(module_name)
            
            if not (module_exists or container_has_service):
                missing_optional.append(f"{module_name} ({description})")

        if missing_optional:
            self.logger.info(
                f"Module {self.name} missing optional modules: {', '.join(missing_optional)}"
            )

    async def _validate_dependencies(self) -> None:
        """Validate component dependencies."""
        if not self.container:
            return
            
        missing = []
        for dep in self.required_dependencies:
            if not self.container.has_service(dep) and dep not in self._modules:
                missing.append(dep)
                
        if missing:
            raise DependencyError(f"Missing required dependencies: {', '.join(missing)}")

    def get_status(self) -> Dict[str, Any]:
        """Get module status information.
        
        Returns:
            Dict containing status information including initialization state
        """
        return {
            "name": self.name,
            "initialized": self._initialized,
            "module_type": self.__class__.__name__,
            "has_container": self.container is not None
        }

def initialize_module(module_name: str, module_class: Type, container: DependencyContainer) -> bool:
    """Initialize a module and register it with the container."""
    try:
        logger.debug(f"Initializing module {module_name}...")
        
        # Create and initialize the module
        module = module_class(name=module_name, config=container.config, container=container)
        asyncio.run(module.initialize())
        
        # Register with container
        container.register_service(module_name, module)
        
        # Log success at debug level - higher level logs should come from the initialization module
        logger.debug(f"{module_name} initialized successfully")
        return True
        
    except Exception as e:
        logger.error(f"Failed to initialize module {module_name}: {e}")
        return False 