"""MCP (Model Context Protocol) implementation."""

import asyncio
import json
import socket
import uuid
import time
from typing import Dict, Any, Optional, List, Union, AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import Enum
import aiohttp
import subprocess

from core.exceptions import MCPError, MCPConnectionError, MCPProtocolError, MCPToolExecutionError
from core.logging import get_component_logger

_protocol_logger = get_component_logger("MCPTransportSecurity")


def _client_version() -> str:
    """POLYROB version advertised as MCP ``clientInfo.version``. Fail-open to a
    neutral literal so a version-resolution hiccup never breaks the MCP handshake."""
    try:
        from core.version import get_version
        return get_version()
    except Exception:
        return "0"


class _PinnedResolver:
    """aiohttp resolver that pins a hostname to a single pre-validated IP.

    SECURITY (SSRF / DNS rebinding): the connect-time SSRF check resolves the
    host once and validates the resulting IP. This resolver makes aiohttp
    connect to *that exact IP* instead of re-resolving DNS, so a rebind to an
    internal/metadata address between validation and connection cannot redirect
    the socket. The original hostname is preserved for the TLS SNI / Host header
    (aiohttp keeps the request host; only the address is overridden), so HTTPS
    certificate validation still matches the real hostname.

    Any host other than the pinned one resolves to nothing (defensive — a
    redirect-induced lookup to a different host is refused rather than resolved).
    """

    def __init__(self, host: str, ip: str, family: int):
        self._host = host
        self._ip = ip
        self._family = family

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host != self._host:
            # Unknown host (e.g. via a redirect) — refuse to resolve it.
            raise OSError(f"Refusing to resolve non-pinned host: {host}")
        return [
            {
                "hostname": host,
                "host": self._ip,
                "port": port,
                "family": self._family,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        return None


def _validate_and_pin_connector(
    url: str,
    *,
    allow_http: bool = False,
    ssl: bool = True,
    limit: int = 10,
    validate_ssrf: bool = True,
    ttl_dns_cache: Optional[int] = None,
) -> "aiohttp.TCPConnector":
    """Validate ``url`` against SSRF policy at CONNECT time and build a pinned connector.

    Resolves+validates the host once (via ``MCPURLValidator.validate_and_resolve``)
    and returns a ``TCPConnector`` whose resolver pins the validated IP. This is
    the live-path defense against DNS rebinding: even though the server was
    validated at add/update time, DNS is re-checked here, immediately before the
    socket opens, and the connection is bound to the address that was checked.

    When ``validate_ssrf`` is False (trusted operator-configured global servers —
    e.g. a dev localhost server), no validation/pinning is applied
    and a plain connector is returned, preserving legacy behavior. The SSRF
    defense is enabled only for user-registered servers, which is where the
    rebinding threat lives.

    Raises:
        MCPConnectionError: if ``validate_ssrf`` and the URL fails SSRF validation
            (blocked host, blocked/rebound IP, bad scheme, unresolvable).
    """
    from urllib.parse import urlparse
    from tools.mcp.security import MCPURLValidator

    if not validate_ssrf:
        # Trusted server — legacy plain connector (still no redirect-following at
        # the request layer, which is a safe tightening applied unconditionally).
        kwargs: Dict[str, Any] = {"ssl": ssl, "limit": limit}
        if ttl_dns_cache is not None:
            kwargs["ttl_dns_cache"] = ttl_dns_cache
        return aiohttp.TCPConnector(**kwargs)

    validator = MCPURLValidator(allow_http=allow_http)
    is_valid, error, pinned_ip = validator.validate_and_resolve(url)
    if not is_valid or not pinned_ip:
        _protocol_logger.error(
            f"SECURITY: MCP connect-time URL validation failed for {urlparse(url).hostname}: {error}"
        )
        raise MCPConnectionError(f"MCP connect blocked (SSRF protection): {error}")

    host = urlparse(url).hostname or ""
    try:
        family = socket.AF_INET6 if ":" in pinned_ip else socket.AF_INET
    except Exception:
        family = socket.AF_INET

    resolver = _PinnedResolver(host=host, ip=pinned_ip, family=family)
    return aiohttp.TCPConnector(
        ssl=ssl,
        limit=limit,
        resolver=resolver,
        # The host is already an IP behind the resolver; keep DNS cache off so
        # nothing else can re-resolve it.
        use_dns_cache=False,
    )


class MCPMessageType(str, Enum):
    """MCP message types."""
    REQUEST = "request"
    RESPONSE = "response" 
    NOTIFICATION = "notification"


@dataclass
class MCPMessage:
    """Base MCP message."""
    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None


@dataclass
class MCPRequest(MCPMessage):
    """MCP request message."""
    method: str = ""
    params: Optional[Dict[str, Any]] = None
    
    def __post_init__(self):
        if self.id is None:
            self.id = str(uuid.uuid4())


@dataclass
class MCPResponse(MCPMessage):
    """MCP response message."""
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None


@dataclass
class MCPNotification(MCPMessage):
    """MCP notification message."""
    method: str = ""
    params: Optional[Dict[str, Any]] = None
    id: Optional[Union[str, int]] = None  # Notifications don't have IDs


class MCPTransport:
    """Base transport for MCP communication."""

    def __init__(self, timeout: int = 180):
        self.timeout = timeout
        self.logger = get_component_logger(self.__class__.__name__)
        self._closed = False
        # Log timeout for debugging
        self.logger.info(f"🔧 {self.__class__.__name__} initialized with {timeout}s timeout")
    
    async def send_message(self, message: MCPMessage) -> None:
        """Send a message through the transport."""
        raise NotImplementedError
    
    async def receive_message(self) -> Optional[MCPMessage]:
        """Receive a message from the transport."""
        raise NotImplementedError
    
    async def close(self) -> None:
        """Close the transport."""
        self._closed = True
    
    @property
    def is_closed(self) -> bool:
        """Check if transport is closed."""
        return self._closed


class MCPStdioTransport(MCPTransport):
    """STDIO transport for MCP communication."""

    # SECURITY: Allowlist of permitted MCP server commands
    # Only these commands can be executed as MCP servers
    ALLOWED_COMMANDS = {
        "npx": True,       # Node.js package runner (for MCP servers)
        "node": True,      # Node.js runtime
        "python": True,    # Python runtime
        "python3": True,   # Python 3 runtime
        "uvx": True,       # Python uv package runner
    }

    # SECURITY: Blocklist of dangerous patterns - use regex for robust matching
    # These patterns are checked against each individual argument
    BLOCKED_ARG_PATTERNS = [
        r'^-e$',           # node -e 'code' or python -e 'code' (inline execution)
        r'^-c$',           # python -c 'code' (inline execution)
        r'^--eval$',       # node --eval (inline execution)
        r'^--print$',      # node --print (inline execution with output)
        r'^-p$',           # node -p (inline execution with output)
    ]

    # SECURITY: Blocklist of dangerous shell metacharacters in arguments
    BLOCKED_CHARS = frozenset([
        ';',   # Command chaining
        '|',   # Piping
        '&',   # Background/chaining
        '`',   # Command substitution
        '$',   # Variable expansion/command substitution
        '>',   # Output redirection
        '<',   # Input redirection
        '\n',  # Newline (command injection)
        '\r',  # Carriage return
        '\0',  # Null byte
    ])

    @classmethod
    def validate_command(cls, command: List[str]) -> tuple[bool, str]:
        """Validate command against security rules.

        SECURITY: Uses a multi-layer approach:
        1. Whitelist base commands
        2. Block dangerous argument flags (like -e, -c for inline code execution)
        3. Block shell metacharacters that could enable command injection

        Returns:
            Tuple of (is_valid, error_message)
        """
        import re

        if not command:
            return False, "Empty command"

        # Get base command (first element)
        base_cmd = command[0].split("/")[-1]  # Handle full paths

        # Check if base command is in allowlist
        if base_cmd not in cls.ALLOWED_COMMANDS:
            return False, f"Command '{base_cmd}' not in MCP server allowlist. Allowed: {list(cls.ALLOWED_COMMANDS.keys())}"

        # SECURITY FIX: Check each argument individually (not as a joined string)
        for i, arg in enumerate(command):
            # Check for blocked argument patterns (e.g., -e, -c for inline execution)
            for pattern in cls.BLOCKED_ARG_PATTERNS:
                if re.match(pattern, arg, re.IGNORECASE):
                    return False, f"Blocked argument pattern '{arg}' at position {i} - inline code execution not allowed"

            # Check for shell metacharacters that could enable injection
            dangerous_chars = cls.BLOCKED_CHARS.intersection(set(arg))
            if dangerous_chars:
                return False, f"Blocked shell metacharacters {dangerous_chars} in argument at position {i}"

        return True, ""

    def __init__(self, command: List[str], env: Optional[Dict[str, str]] = None, timeout: int = 180):
        """Initialize STDIO transport.

        Args:
            command: Command to execute for MCP server
            env: Environment variables for the process
            timeout: Default timeout in seconds for operations (default: 180s for long-running operations)

        Raises:
            MCPConnectionError: If command fails validation
        """
        super().__init__(timeout)

        # SECURITY: Validate command before storing
        is_valid, error_msg = self.validate_command(command)
        if not is_valid:
            self.logger.error(f"SECURITY: MCP command validation failed: {error_msg}")
            raise MCPConnectionError(f"MCP command validation failed: {error_msg}")

        self.command = command
        self.env = env
        self.process: Optional[subprocess.Popen] = None
        self._read_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._health_task: Optional[asyncio.Task] = None

    async def _monitor_process_health(self) -> None:
        """Monitor subprocess health."""
        while not self._closed:
            try:
                if self.process:
                    # Check if process is still running
                    returncode = self.process.poll()
                    if returncode is not None:
                        self.logger.error(
                            f"❌ MCP subprocess exited with code {returncode}!"
                        )
                        # Try to read stderr for error info
                        if self.process.stderr:
                            try:
                                stderr = self.process.stderr.read()
                                if stderr:
                                    self.logger.error(f"Subprocess stderr: {stderr}")
                            except Exception:
                                pass
                        break

                await asyncio.sleep(5)  # Check every 5s

            except Exception as e:
                self.logger.error(f"Error in health monitor: {e}")
                await asyncio.sleep(5)

    async def _read_stderr(self) -> None:
        """Read stderr for debugging."""
        try:
            if self.process and self.process.stderr:
                while self.process.poll() is None:
                    line = await asyncio.to_thread(self.process.stderr.readline)
                    if line:
                        self.logger.debug(f"MCP stderr: {line.strip()}")
                    else:
                        await asyncio.sleep(0.1)
        except Exception as e:
            self.logger.debug(f"Stderr reader ended: {e}")
    
    async def connect(self) -> None:
        """Start the subprocess and establish connection."""
        try:
            import os
            full_env = {**os.environ, **(self.env or {})}
            
            # Debug logging
            self.logger.debug(f"Starting MCP process with command: {self.command}")
            if self.env:
                self.logger.debug(f"Additional env vars: {list(self.env.keys())}")
            
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=full_env,
                bufsize=0  # Unbuffered for better real-time communication
            )
            
            # Wait a bit for process to start
            await asyncio.sleep(0.1)
            
            # Check if process started successfully
            if self.process.poll() is not None:
                stderr_output = ""
                if self.process.stderr:
                    stderr_output = self.process.stderr.read()
                raise MCPConnectionError(f"Process failed to start: {stderr_output}")
            
            self.logger.info(f"Started MCP STDIO process with PID {self.process.pid}")

            # Start health monitor
            self._health_task = asyncio.create_task(self._monitor_process_health())

            # Start stderr reader task for debugging
            if self.process.stderr:
                asyncio.create_task(self._read_stderr())
            
        except Exception as e:
            raise MCPConnectionError(f"Failed to start STDIO transport: {e}")
    
    async def send_message(self, message: MCPMessage) -> None:
        """Send message to STDIO process."""
        if not self.process or self.process.stdin is None:
            raise MCPConnectionError("STDIO transport not connected")
        
        async with self._write_lock:
            try:
                # Convert message to JSON
                message_dict = {
                    "jsonrpc": message.jsonrpc,
                    "id": message.id
                }
                
                if isinstance(message, MCPRequest):
                    message_dict["method"] = message.method
                    if message.params:
                        message_dict["params"] = message.params
                elif isinstance(message, MCPResponse):
                    if message.result is not None:
                        message_dict["result"] = message.result
                    if message.error is not None:
                        message_dict["error"] = message.error
                elif isinstance(message, MCPNotification):
                    message_dict["method"] = message.method
                    if message.params:
                        message_dict["params"] = message.params
                    # Remove id for notifications
                    message_dict.pop("id", None)
                
                json_str = json.dumps(message_dict) + "\n"
                
                # Write to process stdin
                await asyncio.to_thread(self.process.stdin.write, json_str)
                await asyncio.to_thread(self.process.stdin.flush)
                
                self.logger.debug(f"Sent MCP message: {json_str.strip()}")
                
            except Exception as e:
                raise MCPProtocolError(f"Failed to send message: {e}")
    
    async def receive_message(self, timeout: Optional[float] = None) -> Optional[MCPMessage]:
        """Receive message from STDIO process.
        
        Args:
            timeout: Optional timeout override (uses self.timeout if None)
        
        Returns:
            MCPMessage if received, None if timeout or no message available
        """
        if not self.process or self.process.stdout is None:
            raise MCPConnectionError("STDIO transport not connected")
        
        receive_timeout = timeout if timeout is not None else self.timeout
        
        async with self._read_lock:
            try:
                # Read line from stdout with timeout
                line = await asyncio.wait_for(
                    asyncio.to_thread(self.process.stdout.readline),
                    timeout=receive_timeout
                )
                
                if not line:
                    return None
                
                # Parse JSON
                message_dict = json.loads(line.strip())
                
                # Validate basic structure
                if message_dict.get("jsonrpc") != "2.0":
                    raise MCPProtocolError(f"Invalid JSON-RPC version: {message_dict.get('jsonrpc')}")
                
                # Create appropriate message object
                if "method" in message_dict:
                    if "id" in message_dict:
                        # Request
                        return MCPRequest(
                            id=message_dict["id"],
                            method=message_dict["method"],
                            params=message_dict.get("params")
                        )
                    else:
                        # Notification
                        return MCPNotification(
                            method=message_dict["method"],
                            params=message_dict.get("params")
                        )
                else:
                    # Response
                    return MCPResponse(
                        id=message_dict.get("id"),
                        result=message_dict.get("result"),
                        error=message_dict.get("error")
                    )
                    
            except asyncio.TimeoutError:
                self.logger.debug(f"receive_message timed out after {receive_timeout}s")
                return None
            except json.JSONDecodeError as e:
                raise MCPProtocolError(f"Invalid JSON received: {e}")
            except Exception as e:
                raise MCPProtocolError(f"Failed to receive message: {e}")
    
    async def close(self) -> None:
        """Close the STDIO transport."""
        await super().close()
        
        if self.process:
            try:
                # Terminate process gracefully
                self.process.terminate()
                
                # Wait for process to end
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self.process.wait),
                        timeout=5.0
                    )
                except asyncio.TimeoutError:
                    # Force kill if it doesn't terminate
                    self.process.kill()
                    await asyncio.to_thread(self.process.wait)
                
                self.logger.info(f"Closed MCP STDIO process PID {self.process.pid}")
                
            except Exception as e:
                self.logger.error(f"Error closing STDIO transport: {e}")
            finally:
                self.process = None


class MCPSSETransport(MCPTransport):
    """Server-Sent Events transport for MCP communication."""

    def __init__(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 180,
        message_endpoint: Optional[str] = None,  # FIX #7: Explicit POST endpoint
        allow_http: bool = False,  # SSRF: only relax HTTPS for local dev
        validate_ssrf: bool = False  # SSRF: pin+validate at connect (user servers)
    ):
        super().__init__(timeout)
        self.url = url
        self.headers = headers or {}
        self.message_endpoint = message_endpoint  # User-provided or auto-derived
        self.allow_http = allow_http
        self.validate_ssrf = validate_ssrf
        self.session: Optional[aiohttp.ClientSession] = None
        self._response: Optional[aiohttp.ClientResponse] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        self._session_id: Optional[str] = None  # For servers requiring sessionId
    
    async def connect(self) -> None:
        """Connect to SSE endpoint."""
        try:
            # Generate sessionId for servers that require it (like Anysite)
            import uuid
            self._session_id = str(uuid.uuid4())
            
            # FIX: Increase timeout for SSE connections (they can be slow to establish)
            timeout = aiohttp.ClientTimeout(
                total=self.timeout,
                connect=30,  # 30s for connection
                sock_read=self.timeout  # Full timeout for reading
            )
            
            # SECURITY (SSRF/DNS rebinding): validate the URL at connect time and
            # pin the resolved+validated IP so DNS cannot rebind to an internal
            # address between add-time validation and now.
            connector = _validate_and_pin_connector(
                self.url, allow_http=self.allow_http, ssl=True, limit=10,
                validate_ssrf=self.validate_ssrf, ttl_dns_cache=300
            )

            self.session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=timeout,
                connector=connector
            )

            # FIX: Connect to SSE endpoint with proper headers and sessionId query parameter
            sse_headers = {
                **self.headers,
                'Accept': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive'
            }

            # Add sessionId query parameter for servers that require it
            sse_url = f"{self.url}?sessionId={self._session_id}"

            self.logger.info(f"Connecting to SSE endpoint: {sse_url}")
            self.logger.debug(f"SSE headers: {list(sse_headers.keys())}")

            # SECURITY: do not follow redirects — a 3xx Location could point at an
            # internal/metadata host that bypasses the pinned connector.
            self._response = await self.session.get(
                sse_url,
                headers=sse_headers,
                allow_redirects=False
            )
            
            if self._response.status != 200:
                body = await self._response.text()
                self.logger.error(
                    f"SSE connection failed - Status: {self._response.status}, "
                    f"Body: {body[:500]}"
                )
                raise MCPConnectionError(
                    f"SSE connection failed with status {self._response.status}: {body[:200]}"
                )
            
            # Verify content type
            content_type = self._response.headers.get('Content-Type', '')
            if 'text/event-stream' not in content_type:
                self.logger.warning(
                    f"Unexpected Content-Type: {content_type} (expected text/event-stream)"
                )
            
            # Start reading SSE messages
            self._reader_task = asyncio.create_task(self._read_sse_messages())
            
            self.logger.info(f"✅ Connected to MCP SSE endpoint: {self.url}")
            
        except aiohttp.ClientError as e:
            self.logger.error(f"Network error connecting to SSE endpoint: {e}")
            await self.close()
            raise MCPConnectionError(f"Network error connecting to SSE endpoint: {e}")
        except Exception as e:
            self.logger.error(f"Failed to connect to SSE endpoint: {e}", exc_info=True)
            await self.close()
            raise MCPConnectionError(f"Failed to connect to SSE endpoint: {e}")
    
    async def _read_sse_messages(self) -> None:
        """Read SSE messages and queue them."""
        try:
            if not self._response:
                return
                
            async for line in self._response.content:
                line_str = line.decode('utf-8').strip()
                
                if line_str.startswith('data: '):
                    data = line_str[6:]  # Remove 'data: ' prefix
                    
                    try:
                        message_dict = json.loads(data)
                        
                        # Create message object
                        if "method" in message_dict:
                            if "id" in message_dict:
                                message = MCPRequest(
                                    id=message_dict["id"],
                                    method=message_dict["method"],
                                    params=message_dict.get("params")
                                )
                            else:
                                message = MCPNotification(
                                    method=message_dict["method"],
                                    params=message_dict.get("params")
                                )
                        else:
                            message = MCPResponse(
                                id=message_dict.get("id"),
                                result=message_dict.get("result"),
                                error=message_dict.get("error")
                            )
                        
                        await self._message_queue.put(message)
                        
                    except json.JSONDecodeError as e:
                        self.logger.warning(f"Invalid JSON in SSE message: {e}")
                        
        except Exception as e:
            self.logger.error(f"Error reading SSE messages: {e}")
    
    async def send_message(self, message: MCPMessage) -> None:
        """Send message via HTTP POST to the message endpoint.
        
        For MCP over SSE, messages are sent to a separate POST endpoint
        (typically the same URL without /sse suffix or a /messages endpoint).
        """
        if not self.session:
            raise MCPConnectionError("SSE transport not connected")
        
        try:
            # Convert message to dict
            message_dict = {
                "jsonrpc": message.jsonrpc,
                "id": message.id
            }
            
            if isinstance(message, MCPRequest):
                message_dict["method"] = message.method
                if message.params:
                    message_dict["params"] = message.params
            elif isinstance(message, MCPResponse):
                if message.result is not None:
                    message_dict["result"] = message.result
                if message.error is not None:
                    message_dict["error"] = message.error
            
            # FIX #7: Use explicit message_endpoint if provided, otherwise derive
            if self.message_endpoint:
                # User provided explicit POST endpoint
                post_url = self.message_endpoint
            else:
                # Derive POST endpoint from SSE URL
                post_url = self.url
                
                # If URL ends with /sse, try replacing with /message
                if post_url.endswith('/sse'):
                    post_url = post_url[:-4] + '/message'
                elif post_url.endswith('/events'):
                    post_url = post_url[:-7] + '/message'
            
            # Add sessionId query parameter if we have one
            if self._session_id:
                separator = '&' if '?' in post_url else '?'
                post_url = f"{post_url}{separator}sessionId={self._session_id}"
            
            self.logger.debug(f"Sending message to POST endpoint: {post_url}")
            
            # Send via POST with proper headers
            # SECURITY: don't follow redirects (would bypass the pinned connector).
            async with self.session.post(
                post_url,
                json=message_dict,
                headers={**self.headers, 'Content-Type': 'application/json'},
                allow_redirects=False
            ) as response:
                if response.status not in (200, 202, 204):
                    response_text = await response.text()
                    self.logger.error(
                        f"Failed to send message - Status: {response.status}, "
                        f"Body: {response_text[:200]}"
                    )
                    raise MCPProtocolError(
                        f"Failed to send message, status: {response.status}, body: {response_text[:100]}"
                    )
            
            self.logger.debug(f"✅ Sent MCP message to {post_url}: {message_dict.get('method', 'response')}")
            
        except aiohttp.ClientError as e:
            self.logger.error(f"Network error sending SSE message: {e}")
            raise MCPProtocolError(f"Network error sending SSE message: {e}")
        except Exception as e:
            self.logger.error(f"Failed to send SSE message: {e}", exc_info=True)
            raise MCPProtocolError(f"Failed to send SSE message: {e}")
    
    async def receive_message(self, timeout: Optional[float] = None) -> Optional[MCPMessage]:
        """Receive message from queue.
        
        Args:
            timeout: Optional timeout override (uses self.timeout if None)
        
        Returns:
            MCPMessage if received, None if timeout
        """
        receive_timeout = timeout if timeout is not None else self.timeout
        try:
            return await asyncio.wait_for(
                self._message_queue.get(),
                timeout=receive_timeout
            )
        except asyncio.TimeoutError:
            return None
    
    async def close(self) -> None:
        """Close SSE transport."""
        await super().close()
        
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        
        if self._response:
            self._response.close()
            self._response = None
        
        if self.session:
            await self.session.close()
            self.session = None
        
        self.logger.info("Closed MCP SSE transport")


class MCPHTTPTransport(MCPTransport):
    """HTTP JSON-RPC transport for MCP (request-response, no streaming)."""

    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 180, allow_http: bool = False, validate_ssrf: bool = False):
        super().__init__(timeout)
        self.url = url
        self.headers = headers or {}
        self.allow_http = allow_http  # SSRF: only relax HTTPS for local dev
        self.validate_ssrf = validate_ssrf  # SSRF: pin+validate at connect (user servers)
        self.session: Optional[aiohttp.ClientSession] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._response_futures: Dict[str, asyncio.Future] = {}

    async def connect(self) -> None:
        """Connect HTTP session."""
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            # SECURITY (SSRF/DNS rebinding): validate + pin resolved IP at connect time.
            connector = _validate_and_pin_connector(self.url, allow_http=self.allow_http, ssl=True, validate_ssrf=self.validate_ssrf)

            self.session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=timeout,
                connector=connector
            )
            
            self.logger.info(f"✅ HTTP JSON-RPC transport ready: {self.url}")
            
        except Exception as e:
            await self.close()
            raise MCPConnectionError(f"Failed to initialize HTTP transport: {e}")
    
    async def send_message(self, message: MCPMessage) -> None:
        """Send message via HTTP POST and queue response."""
        if not self.session:
            raise MCPConnectionError("HTTP transport not connected")
        
        try:
            # Convert message to dict
            message_dict = {
                "jsonrpc": message.jsonrpc,
                "id": message.id
            }
            
            if isinstance(message, MCPRequest):
                message_dict["method"] = message.method
                if message.params:
                    message_dict["params"] = message.params
            
            self.logger.debug(f"📤 HTTP POST {message_dict.get('method', 'response')}")
            
            # Send via POST and get immediate response
            # SECURITY: don't follow redirects (would bypass the pinned connector).
            async with self.session.post(
                self.url,
                json=message_dict,
                headers={**self.headers, 'Content-Type': 'application/json'},
                allow_redirects=False
            ) as response:
                if response.status not in (200, 202):
                    response_text = await response.text()
                    self.logger.error(
                        f"HTTP failed - Status: {response.status}, Body: {response_text[:200]}"
                    )
                    raise MCPProtocolError(
                        f"HTTP failed, status: {response.status}, body: {response_text[:100]}"
                    )
                
                # Parse and queue response
                response_data = await response.json()
                response_msg = MCPResponse(
                    id=response_data.get("id"),
                    result=response_data.get("result"),
                    error=response_data.get("error")
                )
                
                # Queue the response for receive_message to pick up
                await self._message_queue.put(response_msg)
                self.logger.debug(f"📥 HTTP response queued")
            
        except aiohttp.ClientError as e:
            self.logger.error(f"Network error in HTTP transport: {e}")
            raise MCPProtocolError(f"Network error: {e}")
        except Exception as e:
            self.logger.error(f"Failed HTTP request: {e}", exc_info=True)
            raise MCPProtocolError(f"Failed HTTP request: {e}")
    
    async def receive_message(self, timeout: Optional[float] = None) -> Optional[MCPMessage]:
        """Receive message from queue."""
        receive_timeout = timeout if timeout is not None else self.timeout
        try:
            return await asyncio.wait_for(
                self._message_queue.get(),
                timeout=receive_timeout
            )
        except asyncio.TimeoutError:
            return None
    
    async def close(self) -> None:
        """Close HTTP transport."""
        await super().close()
        
        if self.session:
            await self.session.close()
            self.session = None
        
        self.logger.info("Closed MCP HTTP transport")


class MCPStreamableHTTPTransport(MCPTransport):
    """
    Streamable HTTP transport for MCP (POST with SSE responses).
    
    This transport type is used by servers like mcp.anysite.io/mcp that:
    - Accept POST requests with JSON-RPC body
    - Return responses as SSE-formatted streams (event: message, data: {...})
    - Require session ID management via mcp-session-id header
    - Require Accept: application/json, text/event-stream header
    """

    def __init__(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 180,
        allow_http: bool = False,  # SSRF: only relax HTTPS for local dev
        validate_ssrf: bool = False  # SSRF: pin+validate at connect (user servers)
    ):
        super().__init__(timeout)
        self.url = url
        self.headers = headers or {}
        self.allow_http = allow_http
        self.validate_ssrf = validate_ssrf
        self.session: Optional[aiohttp.ClientSession] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._session_id: Optional[str] = None  # MCP session ID for stateful connections

    async def connect(self) -> None:
        """Initialize HTTP session for streamable transport."""
        try:
            timeout = aiohttp.ClientTimeout(
                total=self.timeout,
                connect=30
            )
            # SECURITY (SSRF/DNS rebinding): validate + pin resolved IP at connect time.
            connector = _validate_and_pin_connector(self.url, allow_http=self.allow_http, ssl=True, limit=10, validate_ssrf=self.validate_ssrf)

            # Base headers (session ID will be added per-request after initialization)
            base_headers = {
                **self.headers,
                'Accept': 'application/json, text/event-stream',
                'Content-Type': 'application/json'
            }
            
            self.session = aiohttp.ClientSession(
                headers=base_headers,
                timeout=timeout,
                connector=connector
            )
            
            self.logger.info(f"✅ Streamable HTTP transport ready: {self.url}")
            
        except Exception as e:
            await self.close()
            raise MCPConnectionError(f"Failed to initialize Streamable HTTP transport: {e}")
    
    async def send_message(self, message: MCPMessage) -> None:
        """Send message via POST and read SSE-formatted response."""
        if not self.session:
            raise MCPConnectionError("Streamable HTTP transport not connected")
        
        try:
            # Convert message to dict based on type
            message_dict = {"jsonrpc": message.jsonrpc}
            
            # Build request headers - include session ID if we have one
            request_headers = {}
            if self._session_id:
                request_headers['mcp-session-id'] = self._session_id
            
            if isinstance(message, MCPRequest):
                message_dict["id"] = message.id
                message_dict["method"] = message.method
                if message.params:
                    message_dict["params"] = message.params
            elif isinstance(message, MCPNotification):
                # Notifications don't have id field
                message_dict["method"] = message.method
                if message.params:
                    message_dict["params"] = message.params
                # Skip sending response wait for notifications
                self.logger.debug(f"📤 Streamable notification: {message.method}")
                async with self.session.post(self.url, json=message_dict, headers=request_headers, allow_redirects=False) as response:
                    # Capture session ID from response if present
                    if 'mcp-session-id' in response.headers:
                        self._session_id = response.headers['mcp-session-id']
                        self.logger.debug(f"📋 Session ID updated: {self._session_id[:20]}...")
                    if response.status not in (200, 202, 204):
                        response_text = await response.text()
                        self.logger.warning(f"Notification response: {response.status}, {response_text[:100]}")
                return  # No response expected for notifications
            elif isinstance(message, MCPResponse):
                message_dict["id"] = message.id
                if message.result is not None:
                    message_dict["result"] = message.result
                if message.error is not None:
                    message_dict["error"] = message.error
            
            self.logger.debug(f"📤 Streamable POST {message_dict.get('method', 'response')}")
            
            # Send POST and read SSE response
            # SECURITY: don't follow redirects (would bypass the pinned connector).
            async with self.session.post(self.url, json=message_dict, headers=request_headers, allow_redirects=False) as response:
                # Capture session ID from response headers (servers send it on initialize)
                if 'mcp-session-id' in response.headers:
                    self._session_id = response.headers['mcp-session-id']
                    self.logger.info(f"📋 MCP Session ID captured: {self._session_id[:20]}...")
                
                if response.status not in (200, 202):
                    response_text = await response.text()
                    self.logger.error(
                        f"Streamable HTTP failed - Status: {response.status}, Body: {response_text[:200]}"
                    )
                    raise MCPProtocolError(
                        f"Streamable HTTP failed, status: {response.status}"
                    )
                
                # Read response as SSE stream
                content_type = response.headers.get('Content-Type', '')
                
                if 'text/event-stream' in content_type:
                    # Parse SSE response - may have multiple events
                    async for line in response.content:
                        line_str = line.decode('utf-8').strip()
                        
                        if line_str.startswith('data: '):
                            data = line_str[6:]  # Remove 'data: ' prefix
                            try:
                                response_data = json.loads(data)
                                response_msg = MCPResponse(
                                    id=response_data.get("id"),
                                    result=response_data.get("result"),
                                    error=response_data.get("error")
                                )
                                await self._message_queue.put(response_msg)
                                self.logger.debug(f"📥 Streamable response queued")
                                break  # Got our response
                            except json.JSONDecodeError as e:
                                self.logger.warning(f"Invalid JSON in SSE data: {e}")
                else:
                    # Plain JSON response
                    response_data = await response.json()
                    response_msg = MCPResponse(
                        id=response_data.get("id"),
                        result=response_data.get("result"),
                        error=response_data.get("error")
                    )
                    await self._message_queue.put(response_msg)
                    self.logger.debug(f"📥 HTTP response queued")
            
        except aiohttp.ClientError as e:
            self.logger.error(f"Network error in Streamable HTTP transport: {e}")
            raise MCPProtocolError(f"Network error: {e}")
        except Exception as e:
            self.logger.error(f"Failed Streamable HTTP request: {e}", exc_info=True)
            raise MCPProtocolError(f"Failed Streamable HTTP request: {e}")
    
    async def receive_message(self, timeout: Optional[float] = None) -> Optional[MCPMessage]:
        """Receive message from queue."""
        receive_timeout = timeout if timeout is not None else self.timeout
        try:
            return await asyncio.wait_for(
                self._message_queue.get(),
                timeout=receive_timeout
            )
        except asyncio.TimeoutError:
            return None
    
    async def close(self) -> None:
        """Close Streamable HTTP transport."""
        await super().close()
        
        if self.session:
            await self.session.close()
            self.session = None
        
        self.logger.info("Closed MCP Streamable HTTP transport")


class MCPClient:
    """MCP client for handling protocol communication."""
    
    def __init__(self, transport: MCPTransport):
        self.transport = transport
        self.logger = get_component_logger(self.__class__.__name__)
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._capabilities: Dict[str, Any] = {}
        self._tools: List[Dict[str, Any]] = []
        self._resources: List[Dict[str, Any]] = []
        self._connected = False
        self._message_handler_task: Optional[asyncio.Task] = None

        # Health monitoring attributes
        self._message_loop_alive = asyncio.Event()
        self._last_message_time = 0.0
        self._message_loop_restart_count = 0
        self._cleanup_task: Optional[asyncio.Task] = None

        # Item 7F: invoked with the updated resource uri when the server sends
        # notifications/resources/updated. Set by the server manager. Default no-op.
        self._resource_update_handler: Optional[Callable[[str], Any]] = None

    async def connect(self) -> None:
        """Connect and initialize MCP session."""
        try:
            await self.transport.connect()
            
            # Start message handler task
            self._message_handler_task = asyncio.create_task(self.process_messages())
            
            # Send initialize request
            init_request = MCPRequest(
                method="initialize",
                params={
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": True,
                        "resources": True
                    },
                    "clientInfo": {
                        "name": "POLYROB MCP Client",
                        "version": _client_version()
                    }
                }
            )
            
            response = await self._send_request(init_request)
            
            if response.error:
                raise MCPProtocolError(f"Initialization failed: {response.error}")
            
            # Store server capabilities
            if response.result:
                self._capabilities = response.result.get("capabilities", {})
                self.logger.info(f"Server capabilities: {self._capabilities}")
                
            # Send initialized notification
            initialized_notification = MCPNotification(
                method="initialized"
            )
            await self.transport.send_message(initialized_notification)
            
            # Discover tools and resources
            await self._discover_capabilities()

            # Start cleanup task
            self._cleanup_task = asyncio.create_task(self._cleanup_pending_requests())

            self._connected = True
            self.logger.info("MCP client connected and initialized")

        except Exception as e:
            await self.close()
            raise MCPConnectionError(f"Failed to connect MCP client: {e}")
    
    async def _send_request(self, request: MCPRequest, timeout: Optional[float] = None) -> MCPResponse:
        """Send request and wait for response.

        Args:
            request: The MCP request to send
            timeout: Timeout in seconds (defaults to transport timeout)

        Returns:
            MCPResponse from the server

        Raises:
            MCPProtocolError: If request times out or fails
        """
        import time

        if not request.id:
            request.id = str(uuid.uuid4())

        # Use provided timeout or default to transport timeout
        request_timeout = timeout if timeout is not None else self.transport.timeout

        # Create future with timeout tracking
        future = asyncio.Future()
        future._timeout_at = time.time() + request_timeout
        future._request_method = request.method
        self._pending_requests[request.id] = future

        try:
            # Detailed logging showing timeout source for debugging
            timeout_source = "explicit" if timeout is not None else "transport"
            self.logger.info(
                f"📤 Sending request {request.method} (id={request.id[:8]}) "
                f"with {request_timeout}s timeout "
                f"(source: {timeout_source}, transport: {self.transport.timeout}s, "
                f"transport_class: {self.transport.__class__.__name__})"
            )

            await self.transport.send_message(request)

            # Wait with timeout
            start_time = time.time()
            try:
                response = await asyncio.wait_for(future, timeout=request_timeout)
                elapsed = time.time() - start_time
                self.logger.info(f"📥 Received response for {request.method} in {elapsed:.2f}s")
                return response
            except asyncio.TimeoutError:
                elapsed = time.time() - start_time

                # Provide context-aware error hints
                if request_timeout < 60:
                    hint = (
                        f"\n\n💡 Timeout is very short (<60s). For MCP operations, 90-180s is recommended. "
                        f"This may be a configuration issue. "
                        f"Check timeout settings in mcp_config.json for this server."
                    )
                else:
                    hint = (
                        f"\n\n💡 This may be a server issue or the operation is too complex. "
                        f"Consider: (1) Check if MCP server process is running, "
                        f"(2) Try a simpler/smaller operation, (3) Check server logs for errors."
                    )

                error_msg = (
                    f"⏱️ Request {request.method} (id={request.id[:8]}) timed out after {elapsed:.2f}s.\n"
                    f"Timeout configured: {request_timeout}s (source: {timeout_source})\n"
                    f"Transport: {self.transport.__class__.__name__} @ {self.transport.timeout}s\n"
                    f"Pending requests: {len(self._pending_requests)}\n"
                    f"Message loop: {'healthy' if self.is_message_loop_healthy else 'UNHEALTHY'}"
                    f"{hint}"
                )

                self.logger.error(error_msg)
                raise MCPProtocolError(error_msg)

        finally:
            # Always cleanup
            self._pending_requests.pop(request.id, None)
    
    async def _discover_capabilities(self) -> None:
        """Discover available tools and resources with retry logic."""
        max_retries = 3
        retry_delay = 1.0
        
        self.logger.debug(f"Starting capability discovery. Server capabilities: {self._capabilities}")
        
        for attempt in range(max_retries):
            try:
                # List tools  
                # Check if server supports tools (can be True, {}, or any truthy value)
                if "tools" in self._capabilities:
                    self.logger.debug(f"Requesting tools list (attempt {attempt + 1})")
                    tools_request = MCPRequest(method="tools/list")
                    tools_response = await self._send_request(tools_request)
                    
                    self.logger.debug(f"Tools response: {tools_response.result}")
                    
                    if tools_response.result:
                        self._tools = tools_response.result.get("tools", [])
                        self.logger.info(f"Discovered {len(self._tools)} tools")
                        if self._tools:
                            for tool in self._tools[:3]:  # Log first 3 tools
                                self.logger.debug(f"  Tool: {tool.get('name', 'unknown')}")
                
                # List resources
                if "resources" in self._capabilities:
                    self.logger.debug(f"Requesting resources list (attempt {attempt + 1})")
                    resources_request = MCPRequest(method="resources/list")
                    resources_response = await self._send_request(resources_request)
                    
                    if resources_response.result:
                        self._resources = resources_response.result.get("resources", [])
                        self.logger.info(f"Discovered {len(self._resources)} resources")
                
                # If we got tools or don't support tools, we're done
                if self._tools or "tools" not in self._capabilities:
                    break
                    
                # If no tools found but server supports them, wait and retry
                if attempt < max_retries - 1:
                    self.logger.info(f"No tools discovered yet, retrying in {retry_delay}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5  # Exponential backoff
                
            except Exception as e:
                if attempt < max_retries - 1:
                    self.logger.warning(f"Failed to discover capabilities (attempt {attempt + 1}/{max_retries}): {e}")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5
                else:
                    self.logger.warning(f"Failed to discover capabilities after {max_retries} attempts: {e}")

    async def _cleanup_pending_requests(self) -> None:
        """Cleanup stale pending requests."""
        import time

        while self._connected:
            try:
                await asyncio.sleep(10)  # Check every 10s

                current_time = time.time()
                stale_requests = []

                for req_id, future in list(self._pending_requests.items()):
                    # Check if request has timed out
                    if hasattr(future, '_timeout_at'):
                        if current_time > future._timeout_at:
                            stale_requests.append(req_id)

                # Cancel stale requests
                for req_id in stale_requests:
                    future = self._pending_requests.pop(req_id, None)
                    if future and not future.done():
                        method = getattr(future, '_request_method', 'unknown')
                        self.logger.warning(f"Cancelling stale request {req_id[:8]} ({method})")
                        future.cancel()

            except Exception as e:
                self.logger.error(f"Error in cleanup task: {e}")

    async def execute_tool(self, name: str, arguments: Dict[str, Any], timeout: Optional[float] = None) -> Any:
        """Execute a tool.
        
        Args:
            name: Name of the tool to execute
            arguments: Tool arguments
            timeout: Optional timeout in seconds (defaults to transport timeout)
        
        Returns:
            Tool execution result
            
        Raises:
            MCPConnectionError: If client not connected
            MCPToolExecutionError: If tool not found or execution fails
            MCPProtocolError: If request times out
        """
        if not self._connected:
            raise MCPConnectionError("Client not connected")
        
        # Find tool
        tool = None
        for t in self._tools:
            if t.get("name") == name:
                tool = t
                break
        
        if not tool:
            raise MCPToolExecutionError(f"Tool '{name}' not found")
        
        # Execute tool with timeout
        request = MCPRequest(
            method="tools/call",
            params={
                "name": name,
                "arguments": arguments
            }
        )
        
        # Pass timeout to _send_request
        response = await self._send_request(request, timeout=timeout)
        
        if response.error:
            raise MCPToolExecutionError(f"Tool execution failed: {response.error}")
        
        return response.result
    
    async def read_resource(self, uri: str) -> Any:
        """Read a resource."""
        if not self._connected:
            raise MCPConnectionError("Client not connected")
        
        request = MCPRequest(
            method="resources/read",
            params={"uri": uri}
        )
        
        response = await self._send_request(request)

        if response.error:
            raise MCPProtocolError(f"Resource read failed: {response.error}")

        return response.result

    async def subscribe_resource(self, uri: str) -> Any:
        """Subscribe to updates for a resource (Item 7F — ``resources/subscribe``)."""
        if not self._connected:
            raise MCPConnectionError("Client not connected")
        response = await self._send_request(
            MCPRequest(method="resources/subscribe", params={"uri": uri})
        )
        if response.error:
            raise MCPProtocolError(f"Resource subscribe failed: {response.error}")
        return response.result

    async def unsubscribe_resource(self, uri: str) -> Any:
        """Unsubscribe from a resource (Item 7F — ``resources/unsubscribe``)."""
        if not self._connected:
            raise MCPConnectionError("Client not connected")
        response = await self._send_request(
            MCPRequest(method="resources/unsubscribe", params={"uri": uri})
        )
        if response.error:
            raise MCPProtocolError(f"Resource unsubscribe failed: {response.error}")
        return response.result

    async def process_messages(self) -> None:
        """Process incoming messages (should be run as task).

        This runs continuously in the background to receive messages from the MCP server
        and complete pending request futures.
        """
        import time

        # Set health flag at start
        self._message_loop_alive.set()
        self.logger.info("Message processing loop started")

        while not self.transport.is_closed:
            try:
                # Update last message time before receiving
                self._last_message_time = time.time()

                # Use a reasonable timeout for polling to allow periodic checks
                # Individual requests will have their own timeouts via _send_request
                message = await self.transport.receive_message(timeout=30.0)

                if message is None:
                    # Timeout or no message - this is normal, just continue
                    continue

                if isinstance(message, MCPResponse):
                    # Handle response - complete the pending future
                    future = self._pending_requests.get(message.id)
                    if future and not future.done():
                        future.set_result(message)
                        self.logger.debug(f"Completed pending request {message.id}")
                    else:
                        self.logger.warning(f"Received response for unknown or completed request: {message.id}")

                elif isinstance(message, MCPRequest):
                    # Handle incoming request (not common for client)
                    self.logger.warning(f"Received unexpected request: {message.method}")

                elif isinstance(message, MCPNotification):
                    # Handle notification
                    self.logger.debug(f"Received notification: {message.method}")
                    await self._handle_notification(message)

            except MCPProtocolError as e:
                self.logger.error(f"Protocol error in message processing: {e}")
                # Continue processing despite protocol errors
            except Exception as e:
                self.logger.error(f"Error processing message: {e}", exc_info=True)
                await asyncio.sleep(1)  # Brief pause before retrying

        # Loop exited - clear health flag
        self._message_loop_alive.clear()
        self.logger.warning("Message processing loop exited!")

    async def _handle_notification(self, message: "MCPNotification") -> None:
        """Process a server notification (Item 7F). Fail-open.

        ``notifications/resources/updated`` (params ``{"uri": ...}``) is routed to
        ``self._resource_update_handler`` (set by the server manager). Other
        notifications are debug-logged only, as before.
        """
        try:
            method = getattr(message, "method", "") or ""
            if method == "notifications/resources/updated":
                params = getattr(message, "params", None) or {}
                uri = params.get("uri")
                handler = self._resource_update_handler
                if uri and handler is not None:
                    result = handler(uri)
                    if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                        await result
        except Exception as e:
            self.logger.error(f"Error handling notification {getattr(message, 'method', '?')}: {e}")

    @property
    def is_message_loop_healthy(self) -> bool:
        """Check if message loop is running and recent.

        Returns:
            True if the message loop is alive and has processed messages recently
        """
        import time

        if not self._message_loop_alive.is_set():
            return False

        # Check if we received message recently (within 60s)
        time_since_last = time.time() - self._last_message_time
        if time_since_last > 60:
            self.logger.warning(f"No messages for {time_since_last:.1f}s")
            return False

        return True

    async def close(self) -> None:
        """Close the client."""
        self._connected = False
        
        # Cancel message handler task
        if self._message_handler_task and not self._message_handler_task.done():
            self._message_handler_task.cancel()
            try:
                await self._message_handler_task
            except asyncio.CancelledError:
                pass
        
        # Cancel pending requests
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()
        
        await self.transport.close()
        self.logger.info("MCP client closed")
    
    @property
    def capabilities(self) -> Dict[str, Any]:
        """Get server capabilities."""
        return self._capabilities
    
    @property
    def tools(self) -> List[Dict[str, Any]]:
        """Get available tools."""
        return self._tools
    
    @property
    def resources(self) -> List[Dict[str, Any]]:
        """Get available resources."""
        return self._resources
    
    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._connected and not self.transport.is_closed
