# Utils Package - Utility Framework

_Last reviewed: 2026-06-30. For the authoritative architecture see ../AGENTS.md; for env flags see ../docs/CONFIGURATION.md._

## Overview

The `utils` package provides a comprehensive collection of utility functions, helpers, and components that support the POLYROB platform's core functionality. It implements essential utilities for rate limiting, user management, message formatting, performance monitoring, and system telemetry.

## Architecture Philosophy

- **Reusable Components**: Common functionality abstracted into reusable utilities
- **Performance Focus**: Optimized utilities for high-performance operations
- **Safety First**: Robust error handling and input validation
- **Consistency**: Standardized interfaces and behavior patterns
- **Modularity**: Self-contained utilities with minimal dependencies
- **Extensibility**: Easy to extend and customize for specific needs

## Package Structure

```
utils/
├── __init__.py                     # Utility exports and registry
├── README.md                       # This documentation
│
├── rate_limit_manager.py           # Comprehensive rate limiting system
├── user_utils.py                   # User management and validation
├── auth_utils.py                   # Authentication utilities
│
├── markdown_utils.py               # Markdown formatting and escaping
├── message_utils.py                # Message handling and chunking
│
├── time_utils.py                   # Performance timing and measurement
├── metrics.py                      # Application metrics collection
├── circuit_breaker.py              # Circuit breaker pattern
│
├── gif_utils.py                    # GIF generation utilities
├── path_validator.py               # Path validation utilities
├── bounded_collections.py          # Size-limited collections
└── result_size.py                  # Result-size guards
```

## Core Utilities

### 1. RateLimitManager (`rate_limit_manager.py`)

Comprehensive rate limiting system for API calls and service requests.

> **Import note**: `RateLimitManager` is intentionally NOT exported from `utils/__init__.py`
> (to avoid circular-import issues with core modules). Import it directly:
> `from utils.rate_limit_manager import RateLimitManager`.

**Key Features**:
- Service-specific rate limits
- Dynamic configuration
- Initialization mode (higher limits during startup)
- Context management for rate-limited operations
- Burst handling

**Configuration**:
```python
class RateLimitManager(BaseComponent):
    def __init__(self, name: str, config: BotConfig):
        self._service_limits = {
            'twitter': {
                'default': {'window': 900, 'max_requests': 180, 'burst_limit': 10},
                'users': {'window': 900, 'max_requests': 100, 'burst_limit': 5},
                'tweets': {'window': 900, 'max_requests': 180, 'burst_limit': 10}
            }
        }
```

**Usage**:
```python
# Context manager for rate-limited operations
async with rate_limiter.request_context('twitter_search'):
    results = await twitter_api.search(query)

# Direct rate limit checking
if await rate_limiter.check_rate_limit('openai_requests'):
    response = await openai_client.complete(prompt)

# Service-specific execution
result = await rate_limiter.execute_with_rate_limit(
    service='twitter',
    func=api_call,
    endpoint_type='users'
)
```

**Core Methods**:
```python
async def check_rate_limit(
    self,
    operation: str,
    max_requests: Optional[int] = None,
    time_window: Optional[int] = None,
    raise_on_limit: bool = True
) -> bool:
    """Check if operation is within rate limits"""

async def configure_limits(
    self,
    service: str,
    requests_per_minute: int,
    burst_limit: int,
    default_wait: int
) -> None:
    """Configure rate limits for a service"""

async def get_remaining_requests(self, operation: str) -> int:
    """Get remaining requests before rate limit"""

async def get_reset_time(self, operation: str) -> float:
    """Get time until rate limit resets"""
```

### 2. User Utilities (`user_utils.py`)

User management, validation, and data processing.

**User Data Extraction**:
```python
def extract_user_data(user: dict) -> Dict[str, Any]:
    """Extract structured data from user object"""

async def get_or_create_user_from_data(
    data: Dict[str, Any],
    user_manager
) -> Dict[str, Any]:
    """Get or create user profile from data"""

def format_user_display_name(user_data: Dict) -> str:
    """Format user display name"""
```

**Validation Functions**:
```python
def validate_email(email: str) -> Tuple[bool, str]:
    """Validate email format with detailed feedback"""

def validate_wallet_address(address: str) -> Tuple[bool, str]:
    """Validate Ethereum wallet address"""
```

**ID Management**:
```python
def generate_user_id(seed=None) -> str:
    """Generate unique hash-based user ID"""

def is_valid_hash_id(user_id: str) -> bool:
    """Validate hash-based user ID format"""

def get_id_type(user_id: str) -> str:
    """Determine ID type: 'hash_id', 'wallet', 'email', or 'unknown'"""
```

### 3. Authentication Utilities (`auth_utils.py`)

Request-scoped authentication helpers that read identity/role from a FastAPI `Request`.

```python
def get_authenticated_user_id(request: Request) -> str:
    """Get the authenticated user ID from the request"""

def get_user_tier(request: Request) -> str:
    """Get the user's tier"""

def get_user_wallet(request: Request) -> Optional[str]:
    """Get the user's wallet address, if any"""

def is_admin(request: Request) -> bool:
    """Whether the request is from an admin"""

def get_user_role(request: Request) -> str:
    """Get the user's role"""

def is_authenticated(request: Request) -> bool:
    """Whether the request is authenticated"""
```

### 4. Markdown Utilities (`markdown_utils.py`)

Safe markdown formatting and escaping for Telegram.

**Key Features**:
- Safe escaping that preserves formatting intent
- Multiple format support (Markdown, MarkdownV2, HTML)
- Code block preservation
- Already-escaped detection

**Core Functions**:
```python
def format_message_with_markdown(text: str) -> Tuple[str, Optional[ParseMode]]:
    """Format message with safe markdown"""

def escape_markdown(text: str, allow_skip: bool = True) -> str:
    """Escape special characters for Markdown formatting"""

def escape_markdown_v2(text: str, allow_skip: bool = True) -> str:
    """Escape for MarkdownV2 with stricter rules"""

def safe_markdown_message(message, **kwargs) -> Tuple[str, ParseMode]:
    """Format message safely, returning (text, parse_mode)"""
```

### 5. Message Utilities (`message_utils.py`)

Advanced message handling including chunking and safe sending.

**Key Features**:
- Automatic chunking for long messages
- Graceful degradation to plain text
- Paragraph preservation
- Rate-limited sending

**Core Functions**:
```python
def split_long_message(text: str, max_length: int = 4096) -> List[str]:
    """Split long text into chunks"""

def format_long_message(text: str) -> str:
    """Format and clean long message"""

async def send_long_message(
    message,
    text: str,
    parse_mode: Optional[str] = ParseMode.MARKDOWN_V2,
    **kwargs
) -> None:
    """Send long message with automatic chunking"""
```

### 6. Time Utilities (`time_utils.py`)

Performance measurement and timing utilities.

**Decorators** (both take an optional `name` label):
```python
@time_execution_sync('expensive_operation')
def expensive_operation():
    """Automatically timed synchronous function"""

@time_execution_async('async_expensive_operation')
async def async_expensive_operation():
    """Automatically timed async function"""
```

**Helpers**:
```python
get_current_timestamp() -> float
parse_timestamp_to_float(ts_value) -> float
parse_date_to_timestamp(date_string: str) -> int
timestamp_to_date(timestamp: int) -> str
```

### 7. Metrics (`metrics.py`)

Application metrics collection and analysis.

**Features**:
- Performance metrics tracking
- Usage statistics collection
- Health monitoring
- Custom metric framework

`Metrics` is a `BaseComponent` (with async `_initialize()`/`_cleanup()` lifecycle).

```python
class Metrics(BaseComponent):
    def record(self, metric_name: str, value: Any, tags: Optional[Dict[str, str]] = None) -> None:
        """Record a metric value (appended under metric_name)"""

    def get_metrics(self) -> Dict[str, Any]:
        """Get all recorded metrics"""
```

### 8. Circuit Breaker (`circuit_breaker.py`)

Circuit breaker pattern for resilient external calls.

**States**:
- `CLOSED`: Normal operation
- `OPEN`: Failing, rejecting calls
- `HALF_OPEN`: Testing if service recovered

```python
class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        expected_exception: Type[Exception] = Exception,
        success_threshold: int = 2,
        half_open_max_calls: int = 3,
        name: Optional[str] = None,
    )

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with circuit breaker protection"""

    def reset(self):
        """Manually reset circuit breaker"""
```

A blocked call raises `CircuitBreakerError`. A process-wide registry is also
available (`CircuitBreakerRegistry`, `get_circuit_breaker_registry()`,
`get_circuit_breaker(name, **kwargs)`).

**Usage**:
```python
breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=30)

try:
    result = await breaker.call(external_api.fetch, param)
except CircuitBreakerError:
    # Service is down, use fallback
    result = cached_data
```

### 9. GIF Utilities (`gif_utils.py`)

GIF generation for conversation history visualization.

These are synchronous functions.

```python
def create_history_gif(
    history_items: List,
    session_id: str,
    output_path: Optional[str] = None,
    user_id: Optional[str] = None,
    max_screenshots: int = 20
) -> Optional[str]:
    """Create a GIF from history items with screenshots"""

def get_conversation_screenshots(
    history_items: Optional[List],
    session_id: str,
    user_id: Optional[str] = None,
    max_screenshots: int = 20
) -> List[str]:
    """Retrieve screenshot paths from history or session directory"""

def create_gif_with_retry(
    screenshots: List[str],
    output_path: str,
    caption_texts: Optional[List[str]] = None,
    max_retries: int = 3,
    delay_seconds: float = 0.5,
    user_id: Optional[str] = None
) -> bool:
    """Create a GIF from screenshot paths with retry logic"""

def create_text_only_gif(
    output_path: str,
    texts: List[str],
    user_id: Optional[str] = None
) -> bool:
    """Create a text-only GIF when image processing fails"""
```

### 10. Path Validator (`path_validator.py`)

Secure path validation to prevent directory traversal.

The validator is a class (with a module-level accessor and a standalone
`sanitize_filename`).

```python
class PathValidator:
    def __init__(self, allowed_paths: Optional[List[str]] = None): ...

    def is_path_allowed(self, path: str, workspace_dir: Optional[str] = None) -> bool:
        """Whether the path is within an allowed root"""

    def validate_path(self, path: str, workspace_dir: Optional[str] = None) -> str:
        """Validate and return the normalized path (raises if disallowed)"""

    def add_allowed_path(self, path: str) -> None: ...
    def remove_allowed_path(self, path: str) -> None: ...
    def is_safe_filename(self, filename: str) -> bool: ...
    def sanitize_filename(self, filename: str) -> str: ...

def get_path_validator(allowed_paths: Optional[List[str]] = None) -> PathValidator:
    """Get/build a PathValidator"""

def sanitize_filename(filename: str) -> str:
    """Remove dangerous characters from filename (module-level helper)"""
```

### 11. Bounded Collections (`bounded_collections.py`)

Size-limited collections for memory-efficient storage.

```python
class BoundedDict(Generic[K, V]):
    """Dictionary with maximum size limit (FIFO eviction of oldest keys)"""

    def __init__(self, max_size: int = 1000):
        ...

class BoundedSet(Generic[V]):
    """Set with maximum size limit (FIFO eviction)"""

    def __init__(self, max_size: int = 10000):
        ...
```

## Integration Patterns

### Service Integration
```python
class TwitterService:
    def __init__(self, rate_limiter: RateLimitManager):
        self.rate_limiter = rate_limiter
    
    async def search_tweets(self, query: str):
        async with self.rate_limiter.request_context('twitter_search'):
            return await self._api_search(query)
```

### Performance Monitoring
```python
class AgentManager:
    @time_execution_async
    async def process_request(self, request):
        result = await self.agent.process(request)
        
        metrics.record('agent_processing_time', timer.duration)
        
        return result
```

### Error-Resilient Operations
```python
breaker = CircuitBreaker(failure_threshold=3)

async def fetch_with_fallback(url: str):
    try:
        return await breaker.call(http_client.get, url)
    except CircuitBreakerError:
        return await cache.get(url)
```

## Exports

```python
__all__ = [
    # Timing utilities
    'time_execution_sync',
    'time_execution_async',
    
    # Message formatting
    'format_message_with_markdown',
    'split_long_message',
    'format_long_message',
    'send_long_message',
    
    # GIF utilities
    'create_history_gif',
    'get_conversation_screenshots',
    'create_gif_with_retry',
    'create_text_only_gif',
    
    # User utilities
    'extract_user_data',
    'validate_email',
    'validate_wallet_address',
    'generate_user_id',
    'is_valid_hash_id',
    'get_id_type',
    'get_or_create_user_from_data',
    'format_user_display_name'
]
```

## Best Practices

### Utility Development
1. **Single Responsibility**: Each utility should have a single, well-defined purpose
2. **Error Resilience**: Implement comprehensive error handling and fallbacks
3. **Performance Focus**: Optimize for common usage patterns
4. **Consistent Interfaces**: Use consistent parameter and return patterns
5. **Documentation**: Provide clear docstrings and usage examples

### Integration Guidelines
1. **Dependency Injection**: Use dependency injection for utility integration
2. **Configuration**: Make utilities configurable through the config system
3. **Logging**: Use consistent logging patterns across utilities
4. **Testing**: Write comprehensive tests for all utility functions
5. **Monitoring**: Include telemetry and metrics collection

### Performance Guidelines
1. **Caching**: Cache expensive operations appropriately
2. **Async/Await**: Use async patterns for I/O operations
3. **Resource Management**: Properly manage resources and cleanup
4. **Memory Efficiency**: Avoid memory leaks in long-running utilities
5. **Rate Limiting**: Respect external service limits
