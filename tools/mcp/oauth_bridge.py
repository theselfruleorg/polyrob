"""OAuth header injection bridge for MCP server connections (T2.4 Task 2).

Wires the dormant ``tools/oauth/`` OAuth2 library into MCP transport
construction: when a server config carries an ``auth`` block (see
``MCPServerConfig.auth``, T2.4 Task 1) and ``MCP_OAUTH_ENABLED=true``,
:func:`apply_oauth_headers` mints/refreshes an OAuth token via a
module-singleton :class:`~tools.oauth.OAuthManager` (persisted through
:class:`~tools.oauth.file_store.FileTokenStore`, encrypted through
:class:`~tools.mcp.security.MCPEncryption`) and merges an ``Authorization``
header into the server's connection headers.

**Byte-identical by default.** With the flag OFF, or when a server config has
no ``auth`` block, :func:`apply_oauth_headers` returns ``config.headers``
unchanged — every call site behaves exactly as it did before this module
existed.

**user_id ("owning tenant") resolution — read from the actual call chain, not
assumed.** OAuth tokens are stored keyed by ``(user_id, provider)``, so the
``user_id`` passed to :func:`apply_oauth_headers` at each construction site
determines whose token is used:

- **User-registered MCP servers**
  (``tools/mcp/server_manager.py::MCPServerManager._connect_server``):
  connection names for these are shaped ``user_{user_id}::{server_name}`` —
  the SAME convention ``add_server``'s FIX #13 per-user-connection-limit check
  already parses (``name.split("::")[0].replace("user_", "")``). A real,
  per-tenant ``user_id`` is available and is used.
- **Global-config MCP servers** (``config/mcp_config.json``, connection names
  with no ``user_`` prefix): there is NO per-tenant identity at connect time —
  these are operator-configured, instance-wide servers loaded once at
  startup, not attached to any request/session. Their tokens are stored under
  ``user_id=""``, i.e. a global-config server backed by OAuth is effectively
  **instance-scoped** (one shared token per provider for the whole
  deployment), not per-tenant. This is a deliberate, documented choice — not
  an oversight — mirroring how other instance-wide state in this codebase
  (e.g. the anonymous/empty-``user_id`` memory bucket) is handled explicitly
  rather than silently.
- **``tools/mcp/user_mcp_service.py`` verification call sites**
  (``_test_mcp_protocol_with_config``, ``_test_mcp_protocol``): both run
  inside a request already scoped to a real ``user_id`` (the outer
  ``add_server``/``test_connection`` callers have it), so that ``user_id`` is
  threaded through rather than falling back to ``""``. Neither site
  constructs an ``auth`` block today — the per-user DB-backed server schema
  (``modules/database/user_mcp_servers.py``) has no OAuth column yet; Task 1
  only added ``auth`` to ``MCPServerConfig``/``_resolve_one``, i.e. the
  global-config path. Wiring the bridge at these two sites now is therefore a
  no-op in practice (``config.auth`` is always ``None`` there) until a future
  task extends the per-user schema/API to accept an ``auth`` block — at which
  point no further bridge changes are needed, only a config field trip.

**401-retry-once** lives on ``MCPTransport`` (``tools/mcp/protocol.py``) as an
optional ``on_auth_refresh`` constructor callback. This module supplies that
callback (:func:`make_auth_refresh_callback`) for the OAuth path only; the
default (``None``) leaves every transport byte-identical to pre-T2.4
behaviour.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Dict, Optional

from core.env import bool_env
from core.exceptions import ConfigurationError
from tools.mcp.config import MCPServerConfig, MCPServerType

logger = logging.getLogger(__name__)

# server_name -> already logged "auth ignored for stdio" (log once per server,
# not once per connect attempt).
_STDIO_WARNED: set = set()

# Module-singleton OAuthManager, built lazily on first use.
_manager = None


def mcp_oauth_enabled() -> bool:
    """Flag: ``MCP_OAUTH_ENABLED``, default OFF (byte-identical legacy)."""
    return bool_env("MCP_OAUTH_ENABLED", False)


def _get_manager():
    """Return the module-singleton OAuthManager (file-persisted, Fernet-encrypted).

    Built lazily so importing this module never touches disk/keys when the
    flag is off (every real caller already short-circuits before reaching
    here, but tests/introspection may import the module freely).
    """
    global _manager
    if _manager is None:
        from tools.mcp.security import MCPEncryption
        from tools.oauth import OAuthManager
        from tools.oauth.file_store import FileTokenStore

        _manager = OAuthManager(encryption=MCPEncryption(), store=FileTokenStore())
    return _manager


def _provider_name(server_name: str) -> str:
    return f"mcp:{server_name}"


def oauth_applies_to(config: MCPServerConfig) -> bool:
    """True when OAuth header injection (and the 401-retry-once callback) is
    actually relevant for ``config`` — flag ON, an ``auth`` block present, and
    a transport that carries HTTP headers (not stdio). Construction sites use
    this to decide whether to build an ``on_auth_refresh`` callback at all;
    :func:`apply_oauth_headers` re-derives the same condition internally so
    the two can never disagree.
    """
    return bool(mcp_oauth_enabled() and config.auth and config.type != MCPServerType.STDIO)


def _ensure_provider_registered(manager, server_name: str, auth: Dict) -> str:
    """Idempotently (re-)register a ``GenericOAuth2Provider`` for this server's
    ``auth`` block and return its provider name. Registration is cheap
    (``OAuthManager.register`` just replaces a dict entry), so re-registering
    on every call keeps the provider's config in sync with the (possibly
    ``${VAR}``-resolved) config currently in hand rather than caching a stale
    one.
    """
    from tools.oauth import GenericOAuth2Provider

    name = _provider_name(server_name)
    manager.register(GenericOAuth2Provider(name=name, config=auth))
    return name


async def apply_oauth_headers(
    config: MCPServerConfig, user_id: str, server_name: str
) -> Dict[str, str]:
    """Return headers for ``config``, with an OAuth ``Authorization`` header
    merged in when applicable.

    Always returns a NEW dict (a shallow copy of ``config.headers``, even in
    the no-op paths) — never the same object ``config.headers`` points at —
    so a caller mutating the returned headers (e.g. the 401-retry-once path
    rewriting ``Authorization`` in place) can never leak back into the
    ``MCPServerConfig`` (T2.4 review Minor).

    - Flag OFF, or no ``config.auth`` block → content-identical to
      ``config.headers`` (byte-identical to pre-T2.4 behaviour; see above for
      why it's still a copy, not the same object).
    - ``config.type == STDIO`` with an ``auth`` block → auth is meaningless
      for STDIO (env is the stdio auth channel); logs ONCE per ``server_name``
      and returns headers unchanged.
    - Otherwise mints/refreshes a token via the module-singleton OAuthManager
      and returns ``{**config.headers, "Authorization": "<type> <token>"}``.
    - No stored/refreshable token (or a malformed ``auth`` block) → raises
      :class:`core.exceptions.ConfigurationError` — the SAME shape
      ``resolve_config_environment_variables``'s per-server fault isolation
      catches, and the same shape ``MCPServerManager._connect_server``'s
      generic ``except Exception`` branch already turns into "this one server
      failed to connect" — so only THIS server is dropped, never the whole
      MCP config.
    """
    headers = dict(config.headers or {})
    if not mcp_oauth_enabled() or not config.auth:
        return headers

    if config.type == MCPServerType.STDIO:
        if server_name not in _STDIO_WARNED:
            logger.warning(
                "MCP server '%s': auth block ignored for stdio transport "
                "(env is the stdio auth channel)",
                server_name,
            )
            _STDIO_WARNED.add(server_name)
        return headers

    manager = _get_manager()
    try:
        provider_name = _ensure_provider_registered(manager, server_name, config.auth)
        token = await manager.get_token(user_id, provider_name)
    except Exception as e:
        raise ConfigurationError(
            f"MCP server '{server_name}': no usable OAuth token for user "
            f"'{user_id}' ({e})"
        ) from e

    headers["Authorization"] = f"{token.token_type} {token.access_token}"
    return headers


def make_auth_refresh_callback(
    user_id: str, server_name: str
) -> Callable[[], Awaitable[Optional[str]]]:
    """Build the ``on_auth_refresh`` callback threaded into transport
    construction for the 401-retry-once (see ``MCPTransport._send_with_auth_retry``
    in ``tools/mcp/protocol.py``).

    Delegates to :meth:`~tools.oauth.OAuthManager.force_refresh`, which
    unconditionally re-mints the token (via the provider's refresh, ignoring
    local expiry bookkeeping since the SERVER just said 401) and returns the
    new ``Authorization`` header value. ``force_refresh`` is serialized
    per-``(user_id, provider)`` (T2.4 review fast-follow) — concurrent 401s
    from multiple in-flight requests on the same connection collapse into
    exactly ONE provider refresh call rather than racing to burn a
    single-use ``refresh_token`` twice or clobber a freshly-refreshed token
    with a stale one. Raises if no usable token results; the transport treats
    that as "refresh failed, surface the original 401".
    """

    async def _refresh() -> Optional[str]:
        manager = _get_manager()
        provider_name = _provider_name(server_name)
        fresh = await manager.force_refresh(user_id, provider_name)
        return f"{fresh.token_type} {fresh.access_token}"

    return _refresh
