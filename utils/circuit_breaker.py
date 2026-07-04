"""Circuit breaker pattern for preventing cascading failures."""

import asyncio
import time
import logging
from enum import Enum
from typing import Optional, Type, Any, Callable, Dict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"        # Normal operation, requests pass through
    OPEN = "open"           # Failing, reject requests immediately
    HALF_OPEN = "half_open" # Testing recovery, allow limited requests

@dataclass
class CircuitStats:
    """Statistics for circuit breaker monitoring."""
    total_calls: int = 0
    success_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    state_changes: list = field(default_factory=list)

class CircuitBreakerError(Exception):
    """Exception raised when circuit breaker is open."""
    pass

class CircuitBreaker:
    """Circuit breaker for preventing cascading failures.

    The circuit breaker pattern prevents an application from repeatedly
    trying to execute an operation that's likely to fail, allowing it
    to recover and preventing cascading failures.

    States:
    - CLOSED: Normal operation, all requests pass through
    - OPEN: Too many failures, requests are immediately rejected
    - HALF_OPEN: Testing if the service has recovered

    Features:
    - Configurable failure threshold and recovery timeout
    - Support for custom exception types
    - Automatic state transitions
    - Statistics tracking
    - Success threshold for closing from half-open state
    """

    def __init__(self,
                 failure_threshold: int = 5,
                 recovery_timeout: float = 60.0,
                 expected_exception: Type[Exception] = Exception,
                 success_threshold: int = 2,
                 half_open_max_calls: int = 3,
                 name: Optional[str] = None):
        """Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Time in seconds before attempting recovery
            expected_exception: Exception type(s) that trigger the breaker
            success_threshold: Successes needed to close from half-open
            half_open_max_calls: Max concurrent calls in half-open state
            name: Optional name for logging
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        self.success_threshold = success_threshold
        self.half_open_max_calls = half_open_max_calls
        self.name = name or "CircuitBreaker"

        self.state = CircuitState.CLOSED
        self.stats = CircuitStats()
        self._half_open_calls = 0
        self._state_lock = asyncio.Lock()

        self.logger = logging.getLogger(f"{self.__class__.__name__}[{self.name}]")

    @asynccontextmanager
    async def __call__(self):
        """Context manager for circuit breaker protection.

        Yields:
            None when circuit allows the operation

        Raises:
            CircuitBreakerError: When circuit is open
            Exception: Original exception from protected operation
        """
        await self._before_call()

        try:
            yield
            await self._on_success()
        except self.expected_exception as e:
            await self._on_failure(e)
            raise
        except Exception as e:
            # Unexpected exceptions don't trigger the circuit breaker
            self.logger.debug(f"Unexpected exception (not triggering breaker): {e}")
            raise

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute a function with circuit breaker protection.

        Args:
            func: Function to execute
            *args: Positional arguments for func
            **kwargs: Keyword arguments for func

        Returns:
            Result from func

        Raises:
            CircuitBreakerError: When circuit is open
            Exception: Original exception from func
        """
        async with self():
            if asyncio.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            else:
                return await asyncio.to_thread(func, *args, **kwargs)

    async def _before_call(self):
        """Check circuit state before allowing call."""
        async with self._state_lock:
            self.stats.total_calls += 1

            if self.state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    self._transition_to_half_open()
                else:
                    self.stats.rejected_calls += 1
                    time_remaining = self.recovery_timeout - (time.time() - self.stats.last_failure_time)
                    raise CircuitBreakerError(
                        f"Circuit breaker '{self.name}' is OPEN - "
                        f"service unavailable (recovery in {time_remaining:.1f}s)"
                    )

            elif self.state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    self.stats.rejected_calls += 1
                    raise CircuitBreakerError(
                        f"Circuit breaker '{self.name}' is HALF_OPEN - "
                        f"max concurrent calls ({self.half_open_max_calls}) reached"
                    )
                self._half_open_calls += 1

    async def _on_success(self):
        """Handle successful call."""
        async with self._state_lock:
            self.stats.success_calls += 1
            self.stats.last_success_time = time.time()
            self.stats.consecutive_failures = 0
            self.stats.consecutive_successes += 1

            if self.state == CircuitState.HALF_OPEN:
                self._half_open_calls = max(0, self._half_open_calls - 1)
                if self.stats.consecutive_successes >= self.success_threshold:
                    self._transition_to_closed()
                else:
                    self.logger.info(
                        f"Circuit breaker '{self.name}' success in HALF_OPEN state "
                        f"({self.stats.consecutive_successes}/{self.success_threshold})"
                    )

            elif self.state == CircuitState.CLOSED:
                # Reset consecutive success counter in closed state
                if self.stats.consecutive_successes > self.success_threshold * 2:
                    self.stats.consecutive_successes = 0

    async def _on_failure(self, exception: Exception):
        """Handle failed call."""
        async with self._state_lock:
            self.stats.failed_calls += 1
            self.stats.last_failure_time = time.time()
            self.stats.consecutive_failures += 1
            self.stats.consecutive_successes = 0

            self.logger.warning(
                f"Circuit breaker '{self.name}' recorded failure "
                f"({self.stats.consecutive_failures}/{self.failure_threshold}): {exception}"
            )

            if self.state == CircuitState.CLOSED:
                if self.stats.consecutive_failures >= self.failure_threshold:
                    self._transition_to_open()

            elif self.state == CircuitState.HALF_OPEN:
                self._half_open_calls = max(0, self._half_open_calls - 1)
                self._transition_to_open()

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset."""
        if not self.stats.last_failure_time:
            return True
        return time.time() - self.stats.last_failure_time >= self.recovery_timeout

    def _transition_to_closed(self):
        """Transition to CLOSED state."""
        old_state = self.state
        self.state = CircuitState.CLOSED
        self.stats.consecutive_failures = 0
        self._half_open_calls = 0

        self.stats.state_changes.append({
            "from": old_state.value,
            "to": CircuitState.CLOSED.value,
            "timestamp": datetime.now().isoformat(),
            "reason": f"Success threshold ({self.success_threshold}) reached"
        })

        self.logger.info(
            f"Circuit breaker '{self.name}' transitioned from {old_state.value} to CLOSED - "
            f"service recovered"
        )

    def _transition_to_open(self):
        """Transition to OPEN state."""
        old_state = self.state
        self.state = CircuitState.OPEN
        self._half_open_calls = 0

        self.stats.state_changes.append({
            "from": old_state.value,
            "to": CircuitState.OPEN.value,
            "timestamp": datetime.now().isoformat(),
            "reason": f"Failure threshold ({self.failure_threshold}) exceeded"
        })

        self.logger.warning(
            f"Circuit breaker '{self.name}' transitioned from {old_state.value} to OPEN - "
            f"service marked as unavailable for {self.recovery_timeout}s"
        )

    def _transition_to_half_open(self):
        """Transition to HALF_OPEN state."""
        old_state = self.state
        self.state = CircuitState.HALF_OPEN
        self.stats.consecutive_failures = 0
        self.stats.consecutive_successes = 0
        self._half_open_calls = 0

        self.stats.state_changes.append({
            "from": old_state.value,
            "to": CircuitState.HALF_OPEN.value,
            "timestamp": datetime.now().isoformat(),
            "reason": f"Recovery timeout ({self.recovery_timeout}s) elapsed"
        })

        self.logger.info(
            f"Circuit breaker '{self.name}' transitioned from {old_state.value} to HALF_OPEN - "
            f"testing service recovery"
        )

    def get_state(self) -> CircuitState:
        """Get current circuit state."""
        return self.state

    def get_stats(self) -> Dict[str, Any]:
        """Get circuit breaker statistics."""
        success_rate = 0
        if self.stats.total_calls > 0:
            success_rate = (self.stats.success_calls / self.stats.total_calls) * 100

        return {
            "name": self.name,
            "state": self.state.value,
            "total_calls": self.stats.total_calls,
            "success_calls": self.stats.success_calls,
            "failed_calls": self.stats.failed_calls,
            "rejected_calls": self.stats.rejected_calls,
            "success_rate": f"{success_rate:.2f}%",
            "consecutive_failures": self.stats.consecutive_failures,
            "consecutive_successes": self.stats.consecutive_successes,
            "last_failure": datetime.fromtimestamp(self.stats.last_failure_time).isoformat()
                            if self.stats.last_failure_time else None,
            "last_success": datetime.fromtimestamp(self.stats.last_success_time).isoformat()
                            if self.stats.last_success_time else None,
            "state_changes": self.stats.state_changes[-10:]  # Last 10 state changes
        }

    def reset(self):
        """Manually reset the circuit breaker to closed state."""
        self.state = CircuitState.CLOSED
        self.stats = CircuitStats()
        self._half_open_calls = 0
        self.logger.info(f"Circuit breaker '{self.name}' manually reset to CLOSED state")

    def force_open(self):
        """Manually open the circuit breaker."""
        old_state = self.state
        self.state = CircuitState.OPEN
        self.stats.last_failure_time = time.time()

        self.stats.state_changes.append({
            "from": old_state.value,
            "to": CircuitState.OPEN.value,
            "timestamp": datetime.now().isoformat(),
            "reason": "Manually forced open"
        })

        self.logger.warning(f"Circuit breaker '{self.name}' manually forced to OPEN state")

class CircuitBreakerRegistry:
    """Registry for managing multiple circuit breakers."""

    def __init__(self):
        """Initialize the registry."""
        self._breakers: Dict[str, CircuitBreaker] = {}
        self.logger = logging.getLogger(self.__class__.__name__)

    def get_or_create(self,
                      name: str,
                      failure_threshold: int = 5,
                      recovery_timeout: float = 60.0,
                      **kwargs) -> CircuitBreaker:
        """Get existing or create new circuit breaker.

        Args:
            name: Unique name for the circuit breaker
            failure_threshold: Number of failures before opening
            recovery_timeout: Recovery timeout in seconds
            **kwargs: Additional arguments for CircuitBreaker

        Returns:
            CircuitBreaker instance
        """
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                **kwargs
            )
            self.logger.info(f"Created new circuit breaker: {name}")

        return self._breakers[name]

    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all circuit breakers.

        Returns:
            Dictionary of breaker name to statistics
        """
        return {
            name: breaker.get_stats()
            for name, breaker in self._breakers.items()
        }

    def reset_all(self):
        """Reset all circuit breakers."""
        for name, breaker in self._breakers.items():
            breaker.reset()
        self.logger.info(f"Reset all {len(self._breakers)} circuit breakers")

# Global registry (singleton pattern)
_global_registry: Optional[CircuitBreakerRegistry] = None

def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    """Get the global circuit breaker registry.

    Returns:
        Global CircuitBreakerRegistry instance
    """
    global _global_registry
    if _global_registry is None:
        _global_registry = CircuitBreakerRegistry()
    return _global_registry

def get_circuit_breaker(name: str, **kwargs) -> CircuitBreaker:
    """Get or create a circuit breaker by name.

    Args:
        name: Name of the circuit breaker
        **kwargs: Configuration for new breaker if created

    Returns:
        CircuitBreaker instance
    """
    registry = get_circuit_breaker_registry()
    return registry.get_or_create(name, **kwargs)