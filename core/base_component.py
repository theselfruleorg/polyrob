"""Location: core/base_component.py"""

"""Base component class for all bot components."""

import logging
import asyncio
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Set, List
from core.config import BotConfig
from core.exceptions import ComponentInitializationError, ConfigurationError, DependencyError
from core.logging import get_component_logger

class BaseComponent(ABC):
    """Base class for all components."""

    def __init__(self, *args, **kwargs):
        """Simplify initialization requirements"""
        self._container = None
        self.name = kwargs.get('name', self.__class__.__name__)  # Default to class name
        self.config = kwargs.get('config')  # Config remains required

        if not self.config:
            raise ValueError("Configuration is required")

        self.logger = get_component_logger(f"{self.__class__.__name__}.{self.name}")
        self.logger.debug(f"Initializing {self.__class__.__name__} with name={self.name}, config={bool(self.config)}")

        self._initialized = False
        self._enabled = True
        self._lock = asyncio.Lock()
        self.logger.debug(f"{self.__class__.__name__} base initialization complete")
        
    async def initialize(self) -> None:
        """Initialize component."""
        self.logger.debug(f"Starting initialization of {self.name}")
        if self._initialized:
            self.logger.debug(f"{self.name} already initialized")
            return
            
        async with self._lock:
            try:
                self.logger.debug(f"Validating dependencies for {self.name}")
                await self._validate_dependencies()
                self.logger.debug(f"Starting component-specific initialization for {self.name}")
                await self._initialize()
                self._initialized = True
                # Log success at INFO level to ensure visibility
                self.logger.info(f"✨ {self.name} initialized successfully")
            except Exception as e:
                self.logger.error(f"Failed to initialize {self.name}: {e}")
                self.logger.error("Stack trace:", exc_info=True)
                raise

    async def _validate_dependencies(self) -> None:
        """Validate component dependencies."""
        if not self.container:
            return
            
        missing = []
        for dep in self.required_dependencies:
            if not self.container.has_service(dep):
                missing.append(dep)
                
        if missing:
            raise DependencyError(f"Missing required dependencies: {', '.join(missing)}")

    @property
    def required_dependencies(self) -> List[str]:
        """Get required dependencies."""
        return []

    @abstractmethod
    async def _initialize(self) -> None:
        """Implementation-specific initialization."""
        pass

    async def cleanup(self) -> None:
        """Clean up component resources."""
        if not self._initialized:
            return
            
        try:
            self.logger.info(f"Cleaning up {self.name}")
            await self._cleanup()
            self._initialized = False
            self.logger.info(f"Successfully cleaned up {self.name}")
        except Exception as e:
            self.logger.error(f"Failed to clean up {self.name}: {e}")
            raise

    @abstractmethod
    async def _cleanup(self) -> None:
        """Implementation-specific cleanup."""
        pass

    @property
    def is_initialized(self) -> bool:
        """Check if component is initialized."""
        return self._initialized

    def _validate_base_config(self) -> None:
        """Validate basic configuration requirements."""
        if not self.config:
            raise ConfigurationError(f"{self.name}: Configuration not provided")
            

    @property
    def container(self):
        if not self._container:
            from core.container import DependencyContainer
            self._container = DependencyContainer.get_instance()
        return self._container

    @container.setter
    def container(self, value):
        self._container = value


# =============================================================================
# ADDED (Dec 13, 2025): Initialization utilities for non-BaseComponent classes
# =============================================================================

def safe_initialize(func):
    """Decorator for async initialization methods with standard error handling.

    Use this on classes that don't inherit from BaseComponent but need
    standardized initialization with:
    - Idempotency check (_initialized flag)
    - Lock protection
    - Error logging and re-raising

    The decorated class must have:
    - self._initialized: bool (set to False in __init__)
    - self._lock: asyncio.Lock (created in __init__)
    - self.logger: logging.Logger (optional, will use module logger if missing)

    Example:
        class MyService:
            def __init__(self):
                self._initialized = False
                self._lock = asyncio.Lock()
                self.logger = logging.getLogger(__name__)

            @safe_initialize
            async def initialize(self):
                # Your initialization logic here
                await self._setup_connections()
    """
    import functools

    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        # Get logger (use module logger if instance doesn't have one)
        logger = getattr(self, 'logger', logging.getLogger(__name__))

        # Check if already initialized
        if getattr(self, '_initialized', False):
            logger.debug(f"{self.__class__.__name__} already initialized")
            return

        # Get or create lock
        lock = getattr(self, '_lock', None)
        if lock is None:
            lock = asyncio.Lock()
            self._lock = lock

        async with lock:
            # Double-check after acquiring lock
            if getattr(self, '_initialized', False):
                return

            try:
                logger.debug(f"Initializing {self.__class__.__name__}")
                result = await func(self, *args, **kwargs)
                self._initialized = True
                logger.info(f"✨ {self.__class__.__name__} initialized successfully")
                return result

            except Exception as e:
                logger.error(f"Failed to initialize {self.__class__.__name__}: {e}")
                raise

    return wrapper


def safe_cleanup(func):
    """Decorator for async cleanup methods with standard error handling.

    Complements safe_initialize for cleanup operations.

    Example:
        class MyService:
            @safe_cleanup
            async def cleanup(self):
                await self._close_connections()
    """
    import functools

    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        logger = getattr(self, 'logger', logging.getLogger(__name__))

        # Only cleanup if initialized
        if not getattr(self, '_initialized', False):
            logger.debug(f"{self.__class__.__name__} not initialized, skipping cleanup")
            return

        try:
            logger.info(f"Cleaning up {self.__class__.__name__}")
            result = await func(self, *args, **kwargs)
            self._initialized = False
            logger.info(f"Successfully cleaned up {self.__class__.__name__}")
            return result

        except Exception as e:
            logger.error(f"Failed to cleanup {self.__class__.__name__}: {e}")
            # Don't re-raise cleanup errors by default to allow other cleanup to proceed
            return None

    return wrapper


class InitializableMixin:
    """Mixin providing standard initialization pattern for any class.

    Use this for classes that can't inherit from BaseComponent but need
    standardized initialization behavior.

    Example:
        class MyService(InitializableMixin):
            async def _do_initialize(self):
                await self._setup_connections()

            async def _do_cleanup(self):
                await self._close_connections()
    """

    _initialized: bool = False
    _lock: asyncio.Lock = None
    logger: logging.Logger = None

    def __init_subclass__(cls, **kwargs):
        """Ensure subclasses have required attributes."""
        super().__init_subclass__(**kwargs)

    def _ensure_mixin_init(self):
        """Ensure mixin attributes are initialized."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        if self.logger is None:
            self.logger = get_component_logger(self.__class__.__name__)

    async def initialize(self) -> None:
        """Initialize with standard pattern."""
        self._ensure_mixin_init()

        if self._initialized:
            self.logger.debug(f"{self.__class__.__name__} already initialized")
            return

        async with self._lock:
            if self._initialized:
                return

            try:
                self.logger.debug(f"Initializing {self.__class__.__name__}")
                await self._do_initialize()
                self._initialized = True
                self.logger.info(f"✨ {self.__class__.__name__} initialized successfully")

            except Exception as e:
                self.logger.error(f"Failed to initialize {self.__class__.__name__}: {e}")
                raise

    async def cleanup(self) -> None:
        """Cleanup with standard pattern."""
        self._ensure_mixin_init()

        if not self._initialized:
            return

        try:
            self.logger.info(f"Cleaning up {self.__class__.__name__}")
            await self._do_cleanup()
            self._initialized = False
            self.logger.info(f"Successfully cleaned up {self.__class__.__name__}")

        except Exception as e:
            self.logger.error(f"Failed to cleanup {self.__class__.__name__}: {e}")

    async def _do_initialize(self) -> None:
        """Override in subclass with initialization logic."""
        pass

    async def _do_cleanup(self) -> None:
        """Override in subclass with cleanup logic."""
        pass

    @property
    def is_initialized(self) -> bool:
        """Check if initialized."""
        return self._initialized