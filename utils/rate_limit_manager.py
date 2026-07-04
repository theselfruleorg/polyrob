"""Location: core/rate_limit_manager.py"""

"""Rate limit manager for API calls."""

import logging
import time
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import asyncio
from contextlib import asynccontextmanager

from core.exceptions import RateLimitError
from core.base_component import BaseComponent
from core.config import BotConfig

class RateLimitManager(BaseComponent):
    """Manager for handling rate limits across different operations."""
    
    def __init__(self, name: str, config: BotConfig, **kwargs):
        """Initialize rate limit manager."""
        super().__init__(name=name, config=config)
        
        # Twitter-specific rate limits
        self._service_limits = {
            'twitter': {
                'default': {
                    'window': 900,  # 15 minutes
                    'max_requests': 180,
                    'burst_limit': 10
                },
                'users': {
                    'window': 900,
                    'max_requests': 100,
                    'burst_limit': 5
                },
                'tweets': {
                    'window': 900,
                    'max_requests': 180,
                    'burst_limit': 10
                }
            }
        }
        
        self._rate_limits = {}
        self._locks = {}
        self._initialization_mode = False
        
        # Use extremely lenient defaults
        self._default_window = kwargs.get('window_seconds', getattr(config, 'rate_limit_window', 1))
        self._default_max_requests = kwargs.get('max_requests', getattr(config, 'rate_limit_max_requests', 100))
        
        # Much higher operation-specific limits
        self._operation_limits = {
            'twitter_init': {
                'window': 1,  # Reduced from 15 * 60
                'max_requests': 100  # Increased from 15
            },
            'twitter_user_tweets': {
                'window': 1,
                'max_requests': 180
            },
            'twitter_search': {
                'window': 1,
                'max_requests': 180
            },
            'default': {
                'window': 1,
                'max_requests': 100
            }
        }
        
        # Much higher limits for initialization
        self._initialization_limits = {
            'twitter_init': {
                'window': 1,
                'max_requests': 1000  # Very high during initialization
            },
            'twitter_user_tweets': {
                'window': 1,
                'max_requests': 1000
            },
            'twitter_search': {
                'window': 1,
                'max_requests': 1000
            },
            'default': {
                'window': 1,
                'max_requests': 1000
            }
        }
        
        self._cache_ttl = getattr(config, 'cache_ttl', 3600)
        self._last_cleanup = time.time()
        self._cleanup_interval = 300
        self._initialized = False
        
    async def initialize(self) -> None:
        """Initialize rate limit manager."""
        if self._initialized:
            return
            
        try:
            await self._initialize()
            self._initialized = True
            self.logger.info(f"{self.name} initialized successfully")
        except Exception as e:
            self.logger.error(f"Failed to initialize {self.name}: {e}")
            raise
        
    async def _initialize(self) -> None:
        """Initialize rate limit manager internals."""
        try:
            # Initialize rate limit tracking
            self._rate_limits = {}
            for service, endpoints in self._service_limits.items():
                for endpoint_type, limits in endpoints.items():
                    operation = f"{service}_{endpoint_type}" if endpoint_type != 'default' else service
                    self._rate_limits[operation] = {
                        'requests': [],
                        'max_requests': limits.get('max_requests', self._default_max_requests),
                        'time_window': limits.get('window', self._default_window)
                    }
                    
            self._locks = {}
            self._last_cleanup = time.time()
            self.logger.info("Rate limit manager initialized successfully")
        except Exception as e:
            self.logger.error(f"Failed to initialize rate limit manager: {e}")
            raise

    async def cleanup(self) -> None:
        """Clean up rate limit manager."""
        if not self._initialized:
            return
            
        try:
            await self._cleanup()
            self._initialized = False
        except Exception as e:
            self.logger.error(f"Failed to clean up rate limit manager: {e}")
            raise
        
    async def _cleanup(self) -> None:
        """Clean up rate limit manager resources."""
        try:
            self._rate_limits.clear()
            self._locks.clear()
            self.logger.info("Rate limit manager cleaned up successfully")
        except Exception as e:
            self.logger.error(f"Error during rate limit manager cleanup: {e}")
            raise

    async def set_initialization_mode(self, enabled: bool = True) -> None:
        """Enable/disable initialization mode with higher limits."""
        self._initialization_mode = enabled
        if enabled:
            self.logger.debug("Rate limit manager in initialization mode - using higher limits")

    async def check_rate_limit(
        self,
        operation: str,
        max_requests: Optional[int] = None,
        time_window: Optional[int] = None,
        raise_on_limit: bool = True
    ) -> bool:
        """Check if operation is within rate limits."""
        try:
            # During initialization, use much higher limits
            if self._initialization_mode:
                op_limits = self._initialization_limits.get(operation, self._initialization_limits['default'])
            else:
                op_limits = self._operation_limits.get(operation, self._operation_limits['default'])

            max_reqs = max_requests or op_limits['max_requests']
            window = time_window or op_limits['window']
            
            # Get or create lock for this operation
            if operation not in self._locks:
                self._locks[operation] = asyncio.Lock()
                
            async with self._locks[operation]:
                # Initialize rate limit tracking for operation if needed
                if operation not in self._rate_limits:
                    self._rate_limits[operation] = {
                        'requests': [],
                        'max_requests': max_reqs,
                        'time_window': window
                    }
                    
                # Get rate limit info
                rate_limit = self._rate_limits[operation]
                current_time = time.time()
                
                # Clean up old requests
                cutoff_time = current_time - window
                rate_limit['requests'] = [
                    t for t in rate_limit['requests'] 
                    if t > cutoff_time
                ]
                
                # Check if we need to do cache cleanup
                if current_time - self._last_cleanup > self._cleanup_interval:
                    await self._cleanup_old_entries()
                    self._last_cleanup = current_time
                
                # Check if we're over the limit
                if len(rate_limit['requests']) >= max_reqs:
                    wait_time = rate_limit['requests'][0] - cutoff_time
                    if self._initialization_mode:
                        # During initialization, just log warning and continue
                        self.logger.warning(
                            f"Rate limit hit during initialization for {operation}. "
                            f"Would normally wait {wait_time:.1f} seconds."
                        )
                        return True
                    elif raise_on_limit:
                        raise RateLimitError(
                            service=operation,
                            wait_time=wait_time,
                            message=f"Rate limit exceeded for {operation}. Please wait {wait_time:.1f} seconds."
                        )
                    return False
                    
                # Add current request
                rate_limit['requests'].append(current_time)
                return True
                
        except RateLimitError:
            if self._initialization_mode:
                self.logger.warning(f"Rate limit error during initialization for {operation}")
                return True
            raise
        except Exception as e:
            self.logger.error(f"Error checking rate limit for {operation}: {e}")
            if self._initialization_mode:
                return True
            if raise_on_limit:
                raise RateLimitError(f"Error checking rate limit: {e}")
            return False

    async def _cleanup_old_entries(self) -> None:
        """Clean up old rate limit entries."""
        try:
            current_time = time.time()
            operations_to_remove = []
            
            for operation, rate_limit in self._rate_limits.items():
                cutoff_time = current_time - rate_limit['time_window']
                rate_limit['requests'] = [
                    t for t in rate_limit['requests'] 
                    if t > cutoff_time
                ]
                
                # If no recent requests, mark for removal
                if not rate_limit['requests']:
                    operations_to_remove.append(operation)
                    
            # Remove empty operations
            for operation in operations_to_remove:
                self._rate_limits.pop(operation, None)
                self._locks.pop(operation, None)
                
            self.logger.debug(
                f"Cleaned up {len(operations_to_remove)} expired rate limit entries"
            )
            
        except Exception as e:
            self.logger.error(f"Error cleaning up rate limit entries: {e}")

    def get_remaining_requests(self, operation: str) -> int:
        """Get number of remaining requests for an operation.
        
        Args:
            operation: Operation identifier
            
        Returns:
            Number of remaining requests, or -1 if operation not found
        """
        try:
            if operation not in self._rate_limits:
                return -1
                
            rate_limit = self._rate_limits[operation]
            current_time = time.time()
            cutoff_time = current_time - rate_limit['time_window']
            
            # Count valid requests
            valid_requests = len([
                t for t in rate_limit['requests']
                if t > cutoff_time
            ])
            
            return max(0, rate_limit['max_requests'] - valid_requests)
            
        except Exception as e:
            self.logger.error(f"Error getting remaining requests for {operation}: {e}")
            return -1

    def get_reset_time(self, operation: str) -> float:
        """Get time until rate limit resets for an operation.
        
        Args:
            operation: Operation identifier
            
        Returns:
            Seconds until reset, or -1 if operation not found
        """
        try:
            if operation not in self._rate_limits:
                return -1
                
            rate_limit = self._rate_limits[operation]
            if not rate_limit['requests']:
                return 0
                
            current_time = time.time()
            oldest_request = min(rate_limit['requests'])
            reset_time = oldest_request + rate_limit['time_window'] - current_time
            
            return max(0, reset_time)
            
        except Exception as e:
            self.logger.error(f"Error getting reset time for {operation}: {e}")
            return -1

    @asynccontextmanager
    async def request_context(
        self,
        operation: str,
        max_requests: Optional[int] = None,
        time_window: Optional[int] = None
    ):
        """Context manager for rate-limited operations."""
        try:
            # Check rate limit before operation
            await self.check_rate_limit(
                operation=operation,
                max_requests=max_requests,
                time_window=time_window,
                raise_on_limit=not self._initialization_mode  # Don't raise during init
            )
            yield
        except RateLimitError as e:
            if self._initialization_mode:
                self.logger.warning(f"Rate limit hit during initialization: {str(e)}")
            else:
                raise
        except Exception as e:
            self.logger.error(f"Error in rate limit context for {operation}: {e}")
            if not self._initialization_mode:
                raise

    async def execute_with_rate_limit(
        self,
        service: str,
        func: callable,
        *args,
        endpoint_type: Optional[str] = None,
        **kwargs
    ) -> Any:
        """Execute with proper rate limiting.
        
        Args:
            service: Service identifier (e.g., 'twitter')
            func: Function to execute
            *args: Arguments to pass to the function
            endpoint_type: Optional endpoint type for specific rate limits
            **kwargs: Keyword arguments to pass to the function
            
        Returns:
            Result of the function execution
            
        Raises:
            RateLimitError: If rate limit is exceeded
        """
        # Get service-specific limits
        service_config = self._service_limits.get(service, {})
        limits = service_config.get(endpoint_type, service_config.get('default', {}))
        
        operation = f"{service}_{endpoint_type}" if endpoint_type else service
        
        async with self.request_context(
            operation=operation,
            max_requests=limits.get('max_requests'),
            time_window=limits.get('window')
        ):
            try:
                if asyncio.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                else:
                    loop = asyncio.get_event_loop()
                    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))
            except Exception as e:
                if "Rate limit exceeded" in str(e):
                    raise RateLimitError(
                        message=f"{service} rate limit exceeded",
                        service=service
                    )
                raise 