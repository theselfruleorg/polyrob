"""
Polymarket Tool - Direct API Integration.

Provides direct integration with Polymarket APIs (Gamma + CLOB)
using native Python API calls.

Architecture:
- Uses httpx for async HTTP requests
- Direct Gamma API integration for market data
- Direct CLOB API integration for trading (via py-clob-client)
- Reuses existing credential storage (PolymarketDBHandler)

Usage by agents:
    Tool action: polymarket.search_markets(query="...")

Agents can call actions directly on this tool.
"""

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pydantic import BaseModel, Field

import httpx

# Authenticated CLOB trading client — sourced from the single adapter seam so a
# missing/incompatible client is a LOUD, typed failure instead of a silent read-only
# degrade. The legacy py-clob-client is archived/non-functional; clob_adapter imports
# the maintained py-clob-client-v2 only.
from tools.polymarket.clob_adapter import (
    ClobClient,
    ApiCreds,
    OrderArgs,
    OrderType,
    CLOB_AVAILABLE,
    trade_capability,
)

# Back-compat alias (some call sites/tests referenced the old flag name).
CLOB_CLIENT_AVAILABLE = CLOB_AVAILABLE

from tools.base_tool import BaseTool, ToolStatus
from tools.polymarket.models import (
    TradingLimits,
    PolymarketCredentials,
    ExecutionResult,
    ApiCredentials,
    POLYGON_MAINNET,
    SIGNATURE_TYPE_PROXY,
    MIN_ORDER_VALUE_USD,
)
from modules.database.polymarket import PolymarketDBHandler
from core.logging import get_component_logger
from core.exceptions import ToolError


# =============================================================================
# PARAMETER MODELS
# =============================================================================

class SearchMarketsParams(BaseModel):
    """Parameters for market search."""
    query: str = Field(..., description="Search query string")
    category: Optional[str] = Field(None, description="Category filter (e.g., 'politics', 'sports', 'crypto')")
    limit: int = Field(10, ge=1, le=100, description="Maximum results to return")
    active_only: bool = Field(True, description="Only return active markets")


class TrendingMarketsParams(BaseModel):
    """Parameters for trending markets."""
    limit: int = Field(10, ge=1, le=50, description="Maximum results to return")
    category: Optional[str] = Field(None, description="Optional category filter")


class MarketDetailsParams(BaseModel):
    """Parameters for market details."""
    market_id: str = Field(..., description="Market slug (e.g., 'will-trump-win-2024') or condition ID (0x...). Use slug from search results.")


class GetPriceParams(BaseModel):
    """Parameters for current price."""
    token_id: str = Field(..., description="Token ID to get price for")
    market_id: Optional[str] = Field(None, description="Market ID for fallback pricing")


class GetOrderbookParams(BaseModel):
    """Parameters for orderbook."""
    token_id: str = Field(..., description="Token ID")
    depth: int = Field(10, ge=1, le=100, description="Orderbook depth")


class GetSpreadParams(BaseModel):
    """Parameters for spread calculation."""
    token_id: str = Field(..., description="Token ID")


class GetVolumeParams(BaseModel):
    """Parameters for market volume."""
    market_id: str = Field(..., description="Market slug (e.g., 'will-trump-win-2024') from search results")


class GetPositionsParams(BaseModel):
    """Parameters for positions."""
    include_closed: bool = Field(False, description="Include closed positions")


class PortfolioSummaryParams(BaseModel):
    """Parameters for portfolio summary."""
    include_positions: bool = Field(True, description="Include position details")


class GetBalanceParams(BaseModel):
    """Parameters for balance check."""
    pass  # No parameters needed, uses authenticated user's wallet


class GetTradeHistoryParams(BaseModel):
    """Parameters for trade history."""
    limit: int = Field(50, ge=1, le=500, description="Maximum trades to return")
    market_id: Optional[str] = Field(None, description="Filter by market ID")


class PlaceLimitOrderParams(BaseModel):
    """Parameters for limit order placement."""
    market_id: str = Field(..., description="Market slug or condition ID (use slug from search results)")
    token_id: str = Field(..., description="Token ID from market outcomes (e.g., '123456789...')")
    side: str = Field(..., description="BUY or SELL")
    price: float = Field(..., ge=0.01, le=0.99, description="Limit price (0.01-0.99)")
    size_usd: float = Field(..., ge=1, description="Order size in USD")


class PlaceMarketOrderParams(BaseModel):
    """Parameters for market order placement."""
    market_id: str = Field(..., description="Market slug or condition ID (use slug from search results)")
    token_id: str = Field(..., description="Token ID from market outcomes (e.g., '123456789...')")
    side: str = Field(..., description="BUY or SELL")
    size_usd: float = Field(..., ge=1, description="Order size in USD")


class CancelOrderParams(BaseModel):
    """Parameters for order cancellation."""
    order_id: str = Field(..., description="Order ID to cancel")


class CancelAllOrdersParams(BaseModel):
    """Parameters for canceling all orders."""
    market_id: Optional[str] = Field(None, description="Cancel only orders in this market")


class GetOpenOrdersParams(BaseModel):
    """Parameters for open orders."""
    market_id: Optional[str] = Field(None, description="Filter by market ID")


class GetOrderHistoryParams(BaseModel):
    """Parameters for order history."""
    limit: int = Field(50, ge=1, le=500, description="Maximum orders to return")
    status: Optional[str] = Field(None, description="Filter by status (filled, cancelled, etc.)")


class FilterByCategoryParams(BaseModel):
    """Parameters for category filtering."""
    category: str = Field(..., description="Category slug")
    limit: int = Field(10, ge=1, le=100, description="Maximum results")
    active_only: bool = Field(True, description="Only active markets")


class ClosingSoonParams(BaseModel):
    """Parameters for closing soon markets."""
    hours: int = Field(24, ge=1, le=168, description="Hours until close")
    limit: int = Field(10, ge=1, le=50, description="Maximum results")


class EmptyParams(BaseModel):
    """Empty parameters for parameterless actions."""
    pass


# =============================================================================
# POLYMARKET SERVICE
# =============================================================================

class PolymarketTool(BaseTool):
    """
    Direct Polymarket API integration tool.

    Provides native Python API access to Polymarket's Gamma and CLOB APIs.

    Tool Categories:
    - Market Discovery: Search, trending, filtering (Gamma API)
    - Market Data: Prices, orderbooks, volume (CLOB + Gamma API)
    - Portfolio: Positions, P&L, history (CLOB API, requires auth)
    - Trading: Orders, cancellations (CLOB API, requires auth)
    """

    tool_id = "polymarket"
    name = "polymarket"
    description = "Polymarket prediction markets - trading and market data"

    # API endpoints
    GAMMA_API_URL = "https://gamma-api.polymarket.com"
    CLOB_API_URL = "https://clob.polymarket.com"
    # Public user-analytics API (positions/trades/leaderboards) — keyed by address,
    # NO auth/signing required. Used for portfolio reads so they need no wallet.
    DATA_API_URL = "https://data-api.polymarket.com"

    def __init__(
        self,
        name: str = "polymarket",
        config: Any = None,
        container: Any = None,
        db_handler: Optional[PolymarketDBHandler] = None
    ):
        super().__init__(name=name, config=config, container=container)
        self.db = db_handler
        self._user_id: Optional[str] = None
        self._credentials_cache: Dict[str, PolymarketCredentials] = {}
        self._http_client: Optional[httpx.AsyncClient] = None

        # CLOB client cache per user (for authenticated operations)
        self._clob_clients: Dict[str, ClobClient] = {}

        # Tool discovery cache
        self._tools_cache: Optional[List[Dict[str, Any]]] = None

        self.logger = get_component_logger("PolymarketTool")

        # Enable by default (no external API key needed for market data)
        self._enabled = True

        # Check if py-clob-client is available
        if not CLOB_AVAILABLE:
            self.logger.warning(
                "polymarket trading client unavailable - trade actions disabled (%s)",
                trade_capability()["install_hint"],
            )
    
    @property
    def required_services(self) -> Dict[str, str]:
        """Get required services."""
        return {
            'rate_limit_manager': 'Rate limit management for API calls'
        }
    
    @property
    def optional_services(self) -> Dict[str, str]:
        """Get optional services."""
        return {
            'cache_manager': 'Cache for market data',
            'database_manager': 'Database for credentials (via polymarket_db)'
        }
    
    async def _initialize(self) -> None:
        """Initialize the service."""
        await super()._initialize()
        
        # Get or create DB handler
        if not self.db and self.container:
            self.db = self.container.get_service('polymarket_db')
            if not self.db:
                # Create it ourselves
                db_manager = self.container.get_service('database_manager')
                if db_manager:
                    from modules.database.polymarket import PolymarketDBHandler
                    self.db = PolymarketDBHandler(db_manager.connection)
                    # Register so API routes can access it
                    self.container.register_service('polymarket_db', self.db)
        
        # Create HTTP client with connection pooling
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=True
        )
        
        # Ensure DB tables exist
        if self.db:
            await self.db.ensure_tables()
        
        self._status = ToolStatus.HEALTHY
        self.logger.info("PolymarketTool initialized (direct API integration)")
    
    async def _cleanup(self) -> None:
        """Cleanup service resources."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        self._credentials_cache.clear()
        self._status = ToolStatus.UNINITIALIZED
        self.logger.info("PolymarketTool cleaned up")
    
    def set_user_context(self, user_id: str) -> None:
        """Set current user for credential resolution."""
        if user_id != self._user_id:
            self._user_id = user_id
            self.logger.debug(f"Polymarket user context set: {user_id}")
    
    async def _get_user_credentials(self) -> Optional[PolymarketCredentials]:
        """Get current user's credentials."""
        if not self._user_id:
            return None
        
        # Check cache first
        if self._user_id in self._credentials_cache:
            return self._credentials_cache[self._user_id]
        
        # Fetch from database
        if self.db:
            credentials = await self.db.get_credentials(self._user_id)
            if credentials:
                self._credentials_cache[self._user_id] = credentials
            return credentials
        
        return None
    
    def _check_trading_limits(
        self,
        limits: TradingLimits,
        size_usd: float
    ) -> Optional[str]:
        """Check if order complies with trading limits."""
        if size_usd > limits.max_order_size_usd:
            return f"Order size ${size_usd:.2f} exceeds limit ${limits.max_order_size_usd}"

        if not limits.enable_autonomous_trading:
            return "Autonomous trading is disabled. Enable in Settings → Polymarket → Trading Limits"

        return None

    async def _check_position_limit(self, limits: TradingLimits, params) -> Optional[str]:
        """Enforce max_position_per_market_usd cumulatively (fail closed).

        _check_trading_limits only bounds a SINGLE order against max_order_size_usd,
        so N sequential sub-limit BUYs on the same market could exceed the per-market
        cap the operator configured. Sum the current USD value of positions in the
        target market and reject when existing + this order would exceed the cap.
        Sell orders de-risk (Polymarket has no shorting) and are exempt. Fail CLOSED
        if the position snapshot can't be fetched (don't open blind).
        """
        cap = getattr(limits, "max_position_per_market_usd", None)
        if not cap:
            return None
        if str(getattr(params, "side", "")).lower() == "sell":
            return None  # reducing exposure
        market_id = getattr(params, "market_id", None)
        if not market_id:
            return None
        res = await self.get_all_positions(GetPositionsParams(include_closed=False))
        if not res.get("success"):
            return "Cannot verify per-market position (position fetch failed); refusing to open"
        existing = sum(
            float(p.get("value", 0) or 0)
            for p in res.get("positions", [])
            if p.get("market_id") == market_id
        )
        projected = existing + float(getattr(params, "size_usd", 0) or 0)
        if projected > cap:
            return (
                f"Per-market position cap ${cap:.2f} would be exceeded on {market_id} "
                f"(existing ${existing:.2f} + ${float(getattr(params, 'size_usd', 0)):.2f})"
            )
        return None

    # =========================================================================
    # CLOB CLIENT MANAGEMENT
    # =========================================================================

    async def _get_clob_client(self) -> Optional[ClobClient]:
        """
        Get authenticated CLOB client for current user with proxy wallet support.

        Creates L1 client with private key, optionally adds L2 API credentials.
        Supports proxy wallets for Polymarket website users (signature_type=2).

        Returns None if user not authenticated or py-clob-client not available.
        """
        if not CLOB_AVAILABLE:
            return None

        if not self._user_id:
            return None

        # Check cache first
        if self._user_id in self._clob_clients:
            return self._clob_clients[self._user_id]

        credentials = await self._get_user_credentials()
        if not credentials or credentials.demo_mode or not credentials.private_key:
            return None

        try:
            # Build client arguments with proxy wallet support
            client_args = {
                "host": self.CLOB_API_URL,
                "chain_id": credentials.chain_id,
                "key": credentials.private_key,
                "signature_type": credentials.signature_type,  # 0=EOA, 1=Magic, 2=Proxy
            }

            # Set funder for proxy wallet users
            # The funder is the address that holds the funds (proxy wallet if set)
            if credentials.proxy_wallet_address:
                client_args["funder"] = credentials.proxy_wallet_address

            # Add L2 credentials if available
            if credentials.api_credentials:
                client_args["creds"] = ApiCreds(
                    api_key=credentials.api_credentials.api_key,
                    api_secret=credentials.api_credentials.api_secret,
                    api_passphrase=credentials.api_credentials.api_passphrase
                )

            client = ClobClient(**client_args)
            self._clob_clients[self._user_id] = client

            sig_type_name = {0: "EOA", 1: "Magic", 2: "Proxy"}.get(credentials.signature_type, "Unknown")
            self.logger.info(
                f"Created CLOB client for user {self._user_id[:8]}... "
                f"(sig_type={sig_type_name}, funder={'proxy' if credentials.proxy_wallet_address else 'eoa'})"
            )
            return client

        except Exception as e:
            self.logger.error(f"Failed to create CLOB client: {e}")
            return None

    async def _ensure_api_credentials(self) -> Optional[ApiCredentials]:
        """
        Ensure user has L2 API credentials, creating them if needed.

        L2 credentials are required for authenticated operations like
        placing orders, viewing positions, etc.

        Returns:
            ApiCredentials if available/created, None otherwise
        """
        if not self._user_id:
            return None

        credentials = await self._get_user_credentials()
        if not credentials or credentials.demo_mode:
            return None

        # Already have API credentials
        if credentials.api_credentials:
            return credentials.api_credentials

        # Need to create new API credentials
        client = await self._get_clob_client()
        if not client:
            return None

        try:
            self.logger.info(
                f"Creating L2 API credentials for user {self._user_id[:8]}... "
                f"(sig_type={credentials.signature_type}, funder={credentials.funder_address[:10] if credentials.funder_address else 'none'}...)"
            )

            # Use create_or_derive_api_key (v2) - works better with proxy wallets
            # This is the method used in the working 15-minute trading script
            api_creds = client.create_or_derive_api_key()

            # Set the API creds on the client (as recommended in docs)
            client.set_api_creds(api_creds)

            # Build our model
            api_credentials = ApiCredentials(
                api_key=api_creds.api_key,
                api_secret=api_creds.api_secret,
                api_passphrase=api_creds.api_passphrase,
                created_at=datetime.utcnow().isoformat()
            )

            # Store in database
            if self.db:
                await self.db.save_api_credentials(self._user_id, api_credentials)

            # Update cache
            credentials.api_credentials = api_credentials
            self._credentials_cache[self._user_id] = credentials

            # Reinitialize client with new credentials
            del self._clob_clients[self._user_id]
            await self._get_clob_client()

            self.logger.info(f"Created API credentials: {api_credentials.api_key[:8]}...")
            return api_credentials

        except Exception as e:
            import traceback
            self.logger.error(
                f"Failed to create API credentials: {e}\n"
                f"Wallet: {credentials.wallet_address}\n"
                f"Proxy: {credentials.proxy_wallet_address}\n"
                f"Sig Type: {credentials.signature_type}\n"
                f"Traceback: {traceback.format_exc()}"
            )
            return None

    async def _get_authenticated_client(self) -> Tuple[Optional[ClobClient], Optional[str]]:
        """
        Get fully authenticated (L2) CLOB client.

        Returns:
            Tuple of (client, error_message). If error, client is None.
        """
        if not CLOB_AVAILABLE:
            return None, f"Polymarket trading client unavailable: {trade_capability()['install_hint']}"

        credentials = await self._get_user_credentials()

        if not credentials:
            return None, "No Polymarket credentials configured. Configure in Settings → Polymarket"

        if credentials.demo_mode:
            return None, "Trading requires wallet configuration (disable demo mode)"

        if not credentials.enabled:
            return None, "Polymarket is disabled for your account"

        if not credentials.private_key:
            return None, "Wallet private key not configured"

        # Ensure API credentials exist
        api_creds = await self._ensure_api_credentials()
        if not api_creds:
            return None, "Failed to create API credentials"

        client = await self._get_clob_client()
        if not client:
            return None, "Failed to initialize CLOB client"

        return client, None

    async def get_balance_and_allowances(self) -> ExecutionResult:
        """
        Get user's USDC balance and allowance status.

        Returns balance in USD and whether allowances are set for trading.
        This uses the correct signature_type for the user's wallet configuration.
        """
        start_time = time.time()

        try:
            client = await self._get_clob_client()
            if not client:
                return ExecutionResult(
                    success=False,
                    error="CLOB client not available. Configure wallet in Settings → Polymarket.",
                    tool_name="get_balance_and_allowances"
                )

            credentials = await self._get_user_credentials()
            if not credentials:
                return ExecutionResult(
                    success=False,
                    error="No credentials configured",
                    tool_name="get_balance_and_allowances"
                )

            # Balance/allowance types come from the same adapter seam.
            from tools.polymarket.clob_adapter import BalanceAllowanceParams, AssetType
            if BalanceAllowanceParams is None or AssetType is None:
                return ExecutionResult(
                    success=False,
                    error=f"Polymarket trading client unavailable: {trade_capability()['install_hint']}",
                    tool_name="get_balance_and_allowances"
                )

            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=credentials.signature_type
            )

            result = client.get_balance_allowance(params)

            balance_wei = int(result.get('balance', 0))
            balance_usd = balance_wei / 1e6  # USDC has 6 decimals

            allowances = result.get('allowances', {})
            allowances_set = all(int(v) > 0 for v in allowances.values()) if allowances else False

            return ExecutionResult(
                success=True,
                data={
                    "balance_usd": balance_usd,
                    "allowances_set": allowances_set,
                    "allowances": {addr: int(v) > 0 for addr, v in allowances.items()},
                    "can_trade": balance_usd >= MIN_ORDER_VALUE_USD and allowances_set,
                    "funder_address": credentials.funder_address,
                    "signature_type": credentials.signature_type,
                },
                tool_name="get_balance_and_allowances",
                execution_time_ms=(time.time() - start_time) * 1000
            )

        except Exception as e:
            self.logger.error(f"Failed to get balance/allowances: {e}")
            return ExecutionResult(
                success=False,
                error=str(e),
                tool_name="get_balance_and_allowances",
                execution_time_ms=(time.time() - start_time) * 1000
            )

    async def _validate_trading_ready(self) -> Tuple[bool, str]:
        """
        Validate that wallet is ready for trading.

        Checks:
        1. CLOB client available
        2. Balance sufficient (>= $1.00)
        3. Allowances set

        Returns:
            (ready: bool, message: str)
        """
        result = await self.get_balance_and_allowances()

        if not result.success:
            return False, f"Failed to check balance: {result.error}"

        data = result.data

        if not data.get("allowances_set"):
            return False, (
                "Trading allowances not set. Please make one trade via "
                "polymarket.com to enable API trading, or set allowances manually."
            )

        balance = data.get("balance_usd", 0)
        if balance < MIN_ORDER_VALUE_USD:
            return False, f"Insufficient balance: ${balance:.2f} (minimum ${MIN_ORDER_VALUE_USD:.2f})"

        return True, f"Ready to trade (balance: ${balance:.2f})"

    def _parse_market_outcomes(self, market: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Parse market outcomes from Gamma API response.

        The Gamma API returns outcomes, outcomePrices, and clobTokenIds as JSON strings.
        This method parses them and combines into a usable format.

        Returns:
            List of outcome dicts with name, token_id, and price
        """
        import json

        outcomes = []

        # Parse JSON strings from Gamma API
        try:
            outcome_names = json.loads(market.get("outcomes", "[]"))
        except (json.JSONDecodeError, TypeError):
            outcome_names = []

        try:
            outcome_prices = json.loads(market.get("outcomePrices", "[]"))
        except (json.JSONDecodeError, TypeError):
            outcome_prices = []

        try:
            token_ids = json.loads(market.get("clobTokenIds", "[]"))
        except (json.JSONDecodeError, TypeError):
            token_ids = []

        # Combine into outcome objects
        for i, name in enumerate(outcome_names):
            outcome = {
                "name": name,
                "token_id": token_ids[i] if i < len(token_ids) else "",
                "price": float(outcome_prices[i]) if i < len(outcome_prices) else 0.0
            }
            outcomes.append(outcome)

        return outcomes

    def _format_market(self, m: Dict[str, Any], include_description: bool = False) -> Dict[str, Any]:
        """
        Format a market from Gamma API response for consistent output.

        Args:
            m: Raw market data from Gamma API
            include_description: Whether to include description (truncated)

        Returns:
            Formatted market dict with parsed outcomes and token_ids
        """
        formatted = {
            "slug": m.get("slug", ""),
            "condition_id": m.get("conditionId") or m.get("id", ""),
            "question": m.get("question", ""),
            "volume": m.get("volume", 0),
            "volume_24h": m.get("volume24hr", 0),
            "liquidity": m.get("liquidity", 0),
            "end_date": m.get("endDate"),
            "active": m.get("active", False),
            "closed": m.get("closed", False),
            "outcomes": self._parse_market_outcomes(m),
        }

        if include_description:
            desc = m.get("description", "") or ""
            formatted["description"] = desc[:500] if len(desc) > 500 else desc

        # Add category from tags if available
        tags = m.get("tags")
        if tags and isinstance(tags, list) and len(tags) > 0:
            formatted["category"] = tags[0].get("label", "")
        else:
            formatted["category"] = ""

        return formatted

    # =========================================================================
    # MARKET DISCOVERY ACTIONS
    # =========================================================================
    
    @BaseTool.action(
        'Search for prediction markets on Polymarket. Returns slug, condition_id, and token_ids for each market.',
        param_model=SearchMarketsParams
    )
    async def search_markets(self, params: SearchMarketsParams) -> Dict[str, Any]:
        """
        Search markets using Gamma API.

        Returns markets with:
        - slug: Use this with get_market_details (e.g., 'will-trump-win-2024')
        - condition_id: The 0x... condition ID
        - outcomes: Each has token_id needed for trading
        """
        await self.ensure_initialized()
        await self.rate_limit("search_markets")
        
        try:
            query_params = {
                "_limit": params.limit,
            }
            
            # Add search query
            if params.query:
                query_params["_q"] = params.query
            
            if params.category:
                query_params["tag_slug"] = params.category
            
            if params.active_only:
                query_params["active"] = "true"
                query_params["closed"] = "false"
            
            response = await self._http_client.get(
                f"{self.GAMMA_API_URL}/markets",
                params=query_params
            )
            response.raise_for_status()
            markets = response.json()

            # Format results using helper (properly parses JSON strings for outcomes/tokens)
            formatted_markets = [
                self._format_market(m, include_description=True) for m in markets
            ]

            return {
                "success": True,
                "count": len(formatted_markets),
                "markets": formatted_markets,
                "note": "Use 'slug' with get_market_details, use 'token_id' from outcomes for trading"
            }
            
        except httpx.HTTPStatusError as e:
            self.logger.error(f"Search markets API error: {e}")
            return {"success": False, "error": f"API error: {e.response.status_code}", "markets": []}
        except Exception as e:
            self.logger.error(f"Search markets failed: {e}")
            return {"success": False, "error": str(e), "markets": []}
    
    @BaseTool.action(
        'Get trending prediction markets by volume',
        param_model=TrendingMarketsParams
    )
    async def get_trending_markets(self, params: TrendingMarketsParams) -> Dict[str, Any]:
        """Get trending markets sorted by 24h volume."""
        await self.ensure_initialized()
        await self.rate_limit("get_trending_markets")
        
        try:
            query_params = {
                "_limit": params.limit,
                "_sort": "-volume24hr",
                "active": "true",
                "closed": "false"
            }
            
            if params.category:
                query_params["tag_slug"] = params.category
            
            response = await self._http_client.get(
                f"{self.GAMMA_API_URL}/markets",
                params=query_params
            )
            response.raise_for_status()
            markets = response.json()

            # Format results using helper (properly parses JSON strings for outcomes/tokens)
            formatted_markets = [self._format_market(m) for m in markets]

            return {
                "success": True,
                "count": len(formatted_markets),
                "sorted_by": "24h_volume",
                "markets": formatted_markets,
                "note": "Use 'slug' with get_market_details, use 'token_id' from outcomes for trading"
            }

        except Exception as e:
            self.logger.error(f"Get trending markets failed: {e}")
            return {"success": False, "error": str(e), "markets": []}
    
    @BaseTool.action(
        'Get markets by category',
        param_model=FilterByCategoryParams
    )
    async def filter_markets_by_category(self, params: FilterByCategoryParams) -> Dict[str, Any]:
        """Filter markets by category slug."""
        await self.ensure_initialized()
        await self.rate_limit("filter_markets_by_category")
        
        try:
            query_params = {
                "_limit": params.limit,
                "tag_slug": params.category,
                "_sort": "-volume24hr"
            }
            
            if params.active_only:
                query_params["active"] = "true"
                query_params["closed"] = "false"
            
            response = await self._http_client.get(
                f"{self.GAMMA_API_URL}/markets",
                params=query_params
            )
            response.raise_for_status()
            markets = response.json()

            # Format results using helper
            formatted_markets = [self._format_market(m) for m in markets]

            return {
                "success": True,
                "category": params.category,
                "count": len(formatted_markets),
                "markets": formatted_markets,
                "note": "Use 'slug' with get_market_details, use 'token_id' from outcomes for trading"
            }

        except Exception as e:
            self.logger.error(f"Filter by category failed: {e}")
            return {"success": False, "error": str(e), "markets": []}
    
    @BaseTool.action(
        'Get featured markets',
        param_model=TrendingMarketsParams
    )
    async def get_featured_markets(self, params: TrendingMarketsParams) -> Dict[str, Any]:
        """Get featured/highlighted markets."""
        await self.ensure_initialized()
        await self.rate_limit("get_featured_markets")

        try:
            query_params = {
                "_limit": params.limit,
                "featured": "true",
                "active": "true",
                "_sort": "-volume24hr"
            }

            response = await self._http_client.get(
                f"{self.GAMMA_API_URL}/markets",
                params=query_params
            )
            response.raise_for_status()
            markets = response.json()

            # Format results using helper
            formatted_markets = [self._format_market(m) for m in markets]

            return {
                "success": True,
                "count": len(formatted_markets),
                "markets": formatted_markets,
                "note": "Use 'slug' with get_market_details, use 'token_id' from outcomes for trading"
            }

        except Exception as e:
            self.logger.error(f"Get featured markets failed: {e}")
            return {"success": False, "error": str(e), "markets": []}
    
    @BaseTool.action(
        'Get markets closing soon',
        param_model=ClosingSoonParams
    )
    async def get_closing_soon_markets(self, params: ClosingSoonParams) -> Dict[str, Any]:
        """
        Get markets closing within specified hours.

        The Gamma API doesn't support date range filters directly,
        so we fetch active markets and filter client-side.
        """
        await self.ensure_initialized()
        await self.rate_limit("get_closing_soon_markets")

        try:
            # Calculate end time threshold
            now = datetime.utcnow()
            end_threshold = now + timedelta(hours=params.hours)

            # Fetch more markets than needed so we can filter
            # We'll request active markets sorted by volume
            query_params = {
                "_limit": 100,  # Fetch more to filter
                "active": "true",
                "closed": "false",
                "_sort": "-volume"  # Sort by volume descending
            }

            response = await self._http_client.get(
                f"{self.GAMMA_API_URL}/markets",
                params=query_params
            )
            response.raise_for_status()
            all_markets = response.json()

            # Filter markets closing within the threshold
            closing_soon = []
            for m in all_markets:
                end_date_str = m.get("endDate")
                if not end_date_str:
                    continue

                try:
                    # Parse ISO date
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    # Make naive for comparison
                    end_date_naive = end_date.replace(tzinfo=None)

                    if now < end_date_naive <= end_threshold:
                        hours_remaining = (end_date_naive - now).total_seconds() / 3600
                        closing_soon.append({
                            "id": m.get("conditionId") or m.get("id"),
                            "question": m.get("question", ""),
                            "end_date": end_date_str,
                            "hours_remaining": round(hours_remaining, 1),
                            "volume": m.get("volume", 0),
                            "volume_24h": m.get("volume24hr", 0),
                            "liquidity": m.get("liquidity", 0)
                        })
                except (ValueError, TypeError):
                    continue

            # Sort by hours remaining and limit
            closing_soon.sort(key=lambda x: x["hours_remaining"])
            closing_soon = closing_soon[:params.limit]

            return {
                "success": True,
                "hours_until_close": params.hours,
                "count": len(closing_soon),
                "markets": closing_soon
            }

        except Exception as e:
            self.logger.error(f"Get closing soon markets failed: {e}")
            return {"success": False, "error": str(e), "markets": []}
    
    @BaseTool.action(
        'Get sports betting markets',
        param_model=TrendingMarketsParams
    )
    async def get_sports_markets(self, params: TrendingMarketsParams) -> Dict[str, Any]:
        """Get sports category markets."""
        return await self.filter_markets_by_category(
            FilterByCategoryParams(category="sports", limit=params.limit, active_only=True)
        )
    
    @BaseTool.action(
        'Get cryptocurrency markets',
        param_model=TrendingMarketsParams
    )
    async def get_crypto_markets(self, params: TrendingMarketsParams) -> Dict[str, Any]:
        """Get crypto category markets."""
        return await self.filter_markets_by_category(
            FilterByCategoryParams(category="crypto", limit=params.limit, active_only=True)
        )
    
    # =========================================================================
    # MARKET DATA ACTIONS
    # =========================================================================
    
    @BaseTool.action(
        'Get detailed market information. Pass the slug from search results (e.g., "will-trump-win-2024").',
        param_model=MarketDetailsParams
    )
    async def get_market_details(self, params: MarketDetailsParams) -> Dict[str, Any]:
        """
        Get comprehensive market details.

        IMPORTANT: Use the 'slug' from search_markets/get_trending_markets results.
        Example: market_id="will-trump-win-2024" (NOT the 0x... condition_id)

        Lookup strategies:
        - /markets/slug/{slug} - for slug-based lookups (preferred)
        - /markets?conditionId={id} - for condition ID lookups
        """
        await self.ensure_initialized()
        await self.rate_limit("get_market_details")

        market_id = params.market_id
        market = None

        try:
            # Strategy 1: If it looks like a slug (no 0x prefix), try slug endpoint
            # Correct endpoint is /markets/slug/{slug} NOT /markets/{slug}
            if not market_id.startswith("0x") and not market_id.isdigit():
                response = await self._http_client.get(
                    f"{self.GAMMA_API_URL}/markets/slug/{market_id}"
                )
                if response.status_code == 200:
                    market = response.json()
                else:
                    self.logger.debug(f"Slug lookup failed with status {response.status_code}")

            # Strategy 2: Search by conditionId (hex string)
            if not market and market_id.startswith("0x"):
                response = await self._http_client.get(
                    f"{self.GAMMA_API_URL}/markets",
                    params={"conditionId": market_id, "_limit": 1}
                )
                if response.status_code == 200:
                    markets = response.json()
                    if markets and len(markets) > 0:
                        market = markets[0]

            # Strategy 3: Search by slug parameter if direct didn't work
            if not market and not market_id.startswith("0x"):
                response = await self._http_client.get(
                    f"{self.GAMMA_API_URL}/markets",
                    params={"slug": market_id, "_limit": 1}
                )
                if response.status_code == 200:
                    markets = response.json()
                    if markets and len(markets) > 0:
                        market = markets[0]

            # Strategy 4: Try CLOB API for market data
            if not market:
                try:
                    clob_response = await self._http_client.get(
                        f"{self.CLOB_API_URL}/markets/{market_id}"
                    )
                    if clob_response.status_code == 200:
                        clob_data = clob_response.json()
                        # CLOB returns different structure, parse tokens
                        tokens = clob_data.get("tokens", [])
                        outcomes = []
                        for t in tokens:
                            outcomes.append({
                                "name": t.get("outcome", ""),
                                "token_id": t.get("token_id", ""),
                                "price": 0  # CLOB tokens may not have price
                            })
                        return {
                            "success": True,
                            "source": "clob",
                            "market": {
                                "id": clob_data.get("condition_id", market_id),
                                "slug": "",
                                "question": clob_data.get("question", ""),
                                "description": clob_data.get("description", ""),
                                "volume": 0,
                                "volume_24h": 0,
                                "liquidity": 0,
                                "end_date": clob_data.get("end_date_iso"),
                                "active": clob_data.get("active", False),
                                "closed": clob_data.get("closed", False),
                                "outcomes": outcomes
                            }
                        }
                except Exception as e:
                    self.logger.debug(f"CLOB lookup failed: {e}")

            if not market:
                return {
                    "success": False,
                    "error": f"Market not found: {market_id}",
                    "suggestion": "Use search_markets to find valid market slugs"
                }

            # Format the response using helper for proper JSON parsing
            formatted = self._format_market(market, include_description=True)

            # Add additional detail fields
            formatted["start_date"] = market.get("startDate")
            formatted["resolved"] = market.get("resolved", False)
            formatted["resolution_source"] = market.get("resolutionSource", "")

            # Add events if present
            events = market.get("events", [])
            if events:
                formatted["events"] = [
                    {
                        "id": e.get("id", ""),
                        "slug": e.get("slug", ""),
                        "title": e.get("title", "")
                    }
                    for e in events
                ]

            return {
                "success": True,
                "source": "gamma",
                "market": formatted,
                "note": "Use 'token_id' from outcomes array for place_limit_order"
            }

        except httpx.HTTPStatusError as e:
            self.logger.error(f"Get market details API error: {e}")
            return {"success": False, "error": f"API error: {e.response.status_code}"}
        except Exception as e:
            self.logger.error(f"Get market details failed: {e}")
            return {"success": False, "error": str(e)}
    
    @BaseTool.action(
        'Get current price for a market token',
        param_model=GetPriceParams
    )
    async def get_current_price(self, params: GetPriceParams) -> Dict[str, Any]:
        """Get current price from CLOB orderbook, fallback to Gamma."""
        await self.ensure_initialized()
        await self.rate_limit("get_current_price")
        
        # Try CLOB first
        try:
            response = await self._http_client.get(
                f"{self.CLOB_API_URL}/price",
                params={"token_id": params.token_id, "side": "BUY"}
            )
            
            if response.status_code == 200:
                data = response.json()
                return {
                    "success": True,
                    "price": data.get("price"),
                    "source": "clob",
                    "token_id": params.token_id
                }
        except Exception as e:
            self.logger.debug(f"CLOB price failed, trying Gamma: {e}")
        
        # Fallback to Gamma API via market details
        if params.market_id:
            try:
                details = await self.get_market_details(MarketDetailsParams(market_id=params.market_id))
                if details.get("success") and details.get("market"):
                    for outcome in details["market"].get("outcomes", []):
                        if outcome.get("token_id") == params.token_id:
                            return {
                                "success": True,
                                "price": outcome.get("price"),
                                "source": "gamma",
                                "token_id": params.token_id
                            }
            except Exception as e:
                self.logger.debug(f"Gamma price fallback failed: {e}")
        
        return {
            "success": False,
            "error": "Price not available - market may be closed or inactive",
            "suggestion": "Try with an active market token_id"
        }
    
    @BaseTool.action(
        'Get orderbook for a market token',
        param_model=GetOrderbookParams
    )
    async def get_orderbook(self, params: GetOrderbookParams) -> Dict[str, Any]:
        """Get orderbook depth from CLOB API."""
        await self.ensure_initialized()
        await self.rate_limit("get_orderbook")
        
        try:
            response = await self._http_client.get(
                f"{self.CLOB_API_URL}/book",
                params={"token_id": params.token_id}
            )
            
            if response.status_code == 404:
                return {
                    "success": False,
                    "error": "Market not found or closed - no active orderbook",
                    "suggestion": "Use an active market token_id"
                }
            
            response.raise_for_status()
            book = response.json()
            
            # Limit to requested depth
            bids = book.get("bids", [])[:params.depth]
            asks = book.get("asks", [])[:params.depth]
            
            # Calculate spread
            spread = None
            if bids and asks:
                best_bid = float(bids[0].get("price", 0))
                best_ask = float(asks[0].get("price", 1))
                if best_bid > 0:
                    spread = (best_ask - best_bid) / best_bid
            
            return {
                "success": True,
                "token_id": params.token_id,
                "bids": bids,
                "asks": asks,
                "spread": spread,
                "spread_percent": f"{spread * 100:.2f}%" if spread else None
            }
            
        except httpx.HTTPStatusError as e:
            return {"success": False, "error": f"CLOB API error: {e.response.status_code}"}
        except Exception as e:
            self.logger.error(f"Get orderbook failed: {e}")
            return {"success": False, "error": str(e)}
    
    @BaseTool.action(
        'Get bid-ask spread for a token',
        param_model=GetSpreadParams
    )
    async def get_spread(self, params: GetSpreadParams) -> Dict[str, Any]:
        """Get bid-ask spread."""
        result = await self.get_orderbook(GetOrderbookParams(token_id=params.token_id, depth=1))
        
        if not result.get("success"):
            return result
        
        return {
            "success": True,
            "token_id": params.token_id,
            "spread": result.get("spread"),
            "spread_percent": result.get("spread_percent"),
            "best_bid": result.get("bids", [{}])[0].get("price") if result.get("bids") else None,
            "best_ask": result.get("asks", [{}])[0].get("price") if result.get("asks") else None
        }
    
    @BaseTool.action(
        'Get market volume statistics',
        param_model=GetVolumeParams
    )
    async def get_market_volume(self, params: GetVolumeParams) -> Dict[str, Any]:
        """Get volume statistics for a market."""
        details = await self.get_market_details(MarketDetailsParams(market_id=params.market_id))
        
        if not details.get("success"):
            return details
        
        market = details.get("market", {})
        return {
            "success": True,
            "market_id": params.market_id,
            "volume_total": market.get("volume", 0),
            "volume_24h": market.get("volume_24h", 0),
            "liquidity": market.get("liquidity", 0)
        }
    
    # =========================================================================
    # PORTFOLIO ACTIONS (Require Authentication)
    # =========================================================================
    
    @BaseTool.action(
        'Get all portfolio positions',
        param_model=GetPositionsParams
    )
    async def get_all_positions(self, params: GetPositionsParams) -> Dict[str, Any]:
        """Get user's portfolio positions via the PUBLIC Data API (no wallet/signing)."""
        await self.ensure_initialized()
        await self.rate_limit("get_all_positions")

        try:
            credentials = await self._get_user_credentials()
            address = getattr(credentials, "funder_address", None) or getattr(credentials, "wallet_address", None)
            if not address:
                return {"success": False, "error": "No wallet address configured for position lookup"}

            # Positions are public by address — query the Data API, no signing.
            resp = await self._http_client.get(
                f"{self.DATA_API_URL}/positions",
                params={"user": address},
            )
            resp.raise_for_status()
            positions_data = resp.json() or []

            # Format positions
            positions = []
            for pos in positions_data:
                positions.append({
                    "market_id": pos.get("market", ""),
                    "token_id": pos.get("asset", ""),
                    "outcome": pos.get("outcome", ""),
                    "size": float(pos.get("size", 0)),
                    "avg_price": float(pos.get("avgPrice", 0)),
                    "current_price": float(pos.get("curPrice", 0)),
                    "cost_basis": float(pos.get("size", 0)) * float(pos.get("avgPrice", 0)),
                    "value": float(pos.get("size", 0)) * float(pos.get("curPrice", 0)),
                    "unrealized_pnl": float(pos.get("pnl", 0)),
                    "realized_pnl": float(pos.get("realizedPnl", 0))
                })

            return {
                "success": True,
                "position_count": len(positions),
                "positions": positions
            }

        except Exception as e:
            self.logger.error(f"Get positions failed: {e}")
            return {"success": False, "error": f"Failed to fetch positions: {str(e)}"}
    
    @BaseTool.action(
        'Get portfolio value and P&L summary',
        param_model=PortfolioSummaryParams
    )
    async def get_portfolio_summary(self, params: PortfolioSummaryParams) -> Dict[str, Any]:
        """Get portfolio summary with P&L calculations."""
        # Delegate to get_all_positions for now
        positions_result = await self.get_all_positions(GetPositionsParams(include_closed=False))
        
        if not positions_result.get("success"):
            return positions_result
        
        # Calculate summary if we have positions
        positions = positions_result.get("positions", [])
        
        total_cost = sum(float(p.get("cost_basis", 0)) for p in positions)
        total_value = sum(float(p.get("value", 0)) for p in positions)
        unrealized_pnl = total_value - total_cost
        
        return {
            "success": True,
            "portfolio_value_usd": total_value,
            "total_cost_basis_usd": total_cost,
            "unrealized_pnl_usd": unrealized_pnl,
            "unrealized_pnl_percent": (unrealized_pnl / total_cost * 100) if total_cost > 0 else 0,
            "position_count": len(positions),
            "positions": positions if params.include_positions else None
        }

    @BaseTool.action(
        'Get wallet balance and allowances',
        param_model=GetBalanceParams
    )
    async def get_balance(self, params: GetBalanceParams) -> Dict[str, Any]:
        """Get user's USDC balance and allowances via authenticated CLOB API."""
        await self.ensure_initialized()
        await self.rate_limit("get_balance")

        client, error = await self._get_authenticated_client()
        if error:
            return {"success": False, "error": error}

        try:
            credentials = await self._get_user_credentials()

            # Get balance and allowance from CLOB API
            balance_data = client.get_balance_allowance()

            return {
                "success": True,
                "wallet_address": credentials.wallet_address,
                "usdc_balance": float(balance_data.get("balance", 0)) / 1e6,  # USDC has 6 decimals
                "usdc_allowance": float(balance_data.get("allowance", 0)) / 1e6,
                "collateral_balance": float(balance_data.get("collateralBalance", 0)) / 1e6,
                "note": "Balances shown in USD (USDC on Polygon)"
            }

        except Exception as e:
            self.logger.error(f"Get balance failed: {e}")
            return {"success": False, "error": f"Failed to fetch balance: {str(e)}"}

    @BaseTool.action(
        'Get trade history',
        param_model=GetTradeHistoryParams
    )
    async def get_trade_history(self, params: GetTradeHistoryParams) -> Dict[str, Any]:
        """Get user's trade history via the PUBLIC Data API (no wallet/signing)."""
        await self.ensure_initialized()
        await self.rate_limit("get_trade_history")

        try:
            credentials = await self._get_user_credentials()
            address = getattr(credentials, "funder_address", None) or getattr(credentials, "wallet_address", None)
            if not address:
                return {"success": False, "error": "No wallet address configured for trade lookup"}

            # Trades are public by address — query the Data API, no signing.
            resp = await self._http_client.get(
                f"{self.DATA_API_URL}/trades",
                params={"user": address, "limit": params.limit},
            )
            resp.raise_for_status()
            trades_data = resp.json() or []

            # Filter by market if specified
            if params.market_id:
                trades_data = [t for t in trades_data if t.get("market") == params.market_id]

            # Limit results
            trades_data = trades_data[:params.limit]

            # Format trades
            trades = []
            for trade in trades_data:
                trades.append({
                    "trade_id": trade.get("id", ""),
                    "market_id": trade.get("market", ""),
                    "token_id": trade.get("asset", ""),
                    "side": trade.get("side", ""),
                    "price": float(trade.get("price", 0)),
                    "size": float(trade.get("size", 0)),
                    "timestamp": trade.get("timestamp"),
                    "fee": float(trade.get("fee", 0))
                })

            return {
                "success": True,
                "trade_count": len(trades),
                "trades": trades
            }

        except Exception as e:
            self.logger.error(f"Get trade history failed: {e}")
            return {"success": False, "error": f"Failed to fetch trades: {str(e)}"}
    
    # =========================================================================
    # TRADING ACTIONS (Require Authentication + Limits Check)
    # =========================================================================
    
    @BaseTool.action(
        'Place a limit order on Polymarket',
        param_model=PlaceLimitOrderParams
    )
    async def place_limit_order(self, params: PlaceLimitOrderParams) -> Dict[str, Any]:
        """Place a limit order via authenticated CLOB API."""
        await self.ensure_initialized()

        if not CLOB_AVAILABLE:
            cap = trade_capability()
            self.logger.warning("polymarket.clob_unavailable: %s", cap["reason"])
            return {
                "success": False,
                "error_code": "POLYMARKET_CLIENT_MISSING",
                "error": f"Polymarket trading client unavailable: {cap['reason']}",
                "suggestion": cap["install_hint"],
            }

        credentials = await self._get_user_credentials()

        if not credentials:
            return {
                "success": False,
                "error": "No Polymarket credentials configured",
                "suggestion": "Configure your wallet in Settings → Polymarket"
            }

        if credentials.demo_mode:
            return {
                "success": False,
                "error": "Cannot place orders in demo mode",
                "suggestion": "Configure your wallet with a private key for trading"
            }

        if not credentials.enabled:
            return {
                "success": False,
                "error": "Polymarket is disabled for your account"
            }

        # Check trading limits
        limits = credentials.trading_limits
        limit_error = self._check_trading_limits(limits, params.size_usd)
        if limit_error:
            return {"success": False, "error": limit_error}

        # Additional confirmation check for large orders
        if params.size_usd > limits.require_confirmation_above_usd:
            return {
                "success": False,
                "error": f"Orders above ${limits.require_confirmation_above_usd} require manual confirmation",
                "order_details": {
                    "market_id": params.market_id,
                    "side": params.side,
                    "price": params.price,
                    "size_usd": params.size_usd
                },
                "suggestion": "Use a smaller order size or adjust your confirmation threshold"
            }

        # N2: route value-moving trades through the wallet PolicyGate (catastrophic
        # ceiling + daily/venue caps + audit). idempotency_key=None: orders may
        # legitimately repeat, so caps/ceiling/audit apply without replay-blocking.
        from core.wallet.factory import get_policy_gate
        policy = get_policy_gate()
        decision = policy.check(venue="polymarket", amount_usd=params.size_usd, idempotency_key=None)
        if not decision.allowed:
            return {"success": False, "error": f"Policy gate denied: {decision.reason}"}

        # T11 live kill-switch: only submit a real order when the master + venue switches
        # are on AND within the live cap; otherwise dry-run (validated, never submitted).
        from tools.crypto_trade_gate import evaluate_live_trade
        gate = evaluate_live_trade("polymarket", params.size_usd)
        if not gate.live:
            return {
                "success": False,
                "dry_run": True,
                "error": f"Order not submitted (dry-run): {gate.reason}",
                "order_details": {
                    "market_id": params.market_id, "token_id": params.token_id,
                    "side": params.side, "price": params.price, "size_usd": params.size_usd,
                },
            }

        # LIVE-only: enforce the cumulative per-market position cap (sell-exempt; fail
        # closed). Placed after the dry-run gate so a dry-run never triggers a live
        # position fetch (and never fail-closes when there is nothing to submit).
        position_error = await self._check_position_limit(limits, params)
        if position_error:
            return {"success": False, "error": position_error}

        await self.rate_limit("place_limit_order")

        client, error = await self._get_authenticated_client()
        if error:
            return {"success": False, "error": error}

        try:
            # Calculate size in shares from USD amount
            size_shares = params.size_usd / params.price

            # Build order arguments
            order_args = OrderArgs(
                token_id=params.token_id,
                price=params.price,
                size=size_shares,
                side=params.side.upper()
            )

            # Create and post order
            result = client.create_and_post_order(order_args)

            policy.record(
                venue="polymarket", action="place_limit_order",
                amount_usd=params.size_usd, counterparty=params.market_id,
                idempotency_key=None, result_ref=str(result.get("orderID"))[:80],
            )

            # Log to audit
            if self.db:
                await self.db.audit_log(
                    self._user_id,
                    "order_placed",
                    tool_name="polymarket",
                    market_id=params.market_id,
                    details={
                        "order_id": result.get("orderID"),
                        "side": params.side,
                        "price": params.price,
                        "size_usd": params.size_usd,
                        "size_shares": size_shares
                    }
                )

            return {
                "success": True,
                "order_id": result.get("orderID"),
                "status": result.get("status", "pending"),
                "order_details": {
                    "market_id": params.market_id,
                    "token_id": params.token_id,
                    "side": params.side,
                    "price": params.price,
                    "size_usd": params.size_usd,
                    "size_shares": size_shares
                }
            }

        except Exception as e:
            self.logger.error(f"Place order failed: {e}")

            # Log failure
            if self.db:
                await self.db.audit_log(
                    self._user_id,
                    "order_failed",
                    tool_name="polymarket",
                    market_id=params.market_id,
                    details={"error": str(e)}
                )

            return {"success": False, "error": f"Order placement failed: {str(e)}"}

    @BaseTool.action(
        'Place a marketable order on Polymarket (crosses the spread for immediate fill)',
        param_model=PlaceMarketOrderParams
    )
    async def place_market_order(self, params: PlaceMarketOrderParams) -> Dict[str, Any]:
        """Marketable order: price aggressively across the spread, then route through the
        same gates as place_limit_order (limits, >$500 confirmation, PolicyGate, exposure)."""
        await self.ensure_initialized()

        price_res = await self.get_current_price(
            GetPriceParams(token_id=params.token_id, market_id=params.market_id)
        )
        mid = price_res.get("price") if price_res.get("success") else None
        if mid is None:
            return {"success": False, "error": "Cannot fetch a market price for the market order"}

        # Cross the spread by a slippage buffer; clamp to the valid 0.01–0.99 band.
        SLIPPAGE = 0.03
        mid = float(mid)
        if params.side.upper() == "BUY":
            limit_price = min(0.99, round(mid * (1 + SLIPPAGE), 2))
        else:
            limit_price = max(0.01, round(mid * (1 - SLIPPAGE), 2))

        return await self.place_limit_order(PlaceLimitOrderParams(
            market_id=params.market_id,
            token_id=params.token_id,
            side=params.side,
            price=limit_price,
            size_usd=params.size_usd,
        ))

    @BaseTool.action(
        'Get open orders',
        param_model=GetOpenOrdersParams
    )
    async def get_open_orders(self, params: GetOpenOrdersParams) -> Dict[str, Any]:
        """Get user's open orders via authenticated CLOB API."""
        await self.ensure_initialized()
        await self.rate_limit("get_open_orders")

        credentials = await self._get_user_credentials()

        if not credentials or credentials.demo_mode:
            return {
                "success": True,
                "total_open_orders": 0,
                "orders": [],
                "note": "Demo mode - no trading orders"
            }

        client, error = await self._get_authenticated_client()
        if error:
            return {"success": False, "error": error}

        try:
            # Get orders from CLOB API
            orders_params = {}
            if params.market_id:
                orders_params["market"] = params.market_id

            orders_data = client.get_open_orders(**orders_params)

            # Format orders
            orders = []
            for order in orders_data:
                orders.append({
                    "order_id": order.get("id", ""),
                    "market_id": order.get("market", ""),
                    "token_id": order.get("asset", ""),
                    "side": order.get("side", ""),
                    "price": float(order.get("price", 0)),
                    "size": float(order.get("size", 0)),
                    "size_matched": float(order.get("sizeMatched", 0)),
                    "status": order.get("status", ""),
                    "created_at": order.get("createdAt")
                })

            return {
                "success": True,
                "total_open_orders": len(orders),
                "orders": orders
            }

        except Exception as e:
            self.logger.error(f"Get open orders failed: {e}")
            return {"success": False, "error": f"Failed to fetch orders: {str(e)}"}
    
    @BaseTool.action(
        'Get order history',
        param_model=GetOrderHistoryParams
    )
    async def get_order_history(self, params: GetOrderHistoryParams) -> Dict[str, Any]:
        """Get user's order history via authenticated CLOB API."""
        await self.ensure_initialized()
        await self.rate_limit("get_order_history")

        credentials = await self._get_user_credentials()

        if not credentials or credentials.demo_mode:
            return {
                "success": True,
                "total_orders": 0,
                "orders": [],
                "note": "Demo mode - no trading orders"
            }

        client, error = await self._get_authenticated_client()
        if error:
            return {"success": False, "error": error}

        try:
            # Get all orders (includes historical)
            # Note: CLOB API may not have direct history endpoint, using get_orders
            orders_data = client.get_open_orders()

            # Filter by status if specified
            if params.status:
                orders_data = [o for o in orders_data if o.get("status") == params.status]

            # Limit results
            orders_data = orders_data[:params.limit]

            # Format orders
            orders = []
            for order in orders_data:
                orders.append({
                    "order_id": order.get("id", ""),
                    "market_id": order.get("market", ""),
                    "token_id": order.get("asset", ""),
                    "side": order.get("side", ""),
                    "price": float(order.get("price", 0)),
                    "size": float(order.get("size", 0)),
                    "size_matched": float(order.get("sizeMatched", 0)),
                    "status": order.get("status", ""),
                    "created_at": order.get("createdAt")
                })

            return {
                "success": True,
                "total_orders": len(orders),
                "orders": orders
            }

        except Exception as e:
            self.logger.error(f"Get order history failed: {e}")
            return {"success": False, "error": f"Failed to fetch order history: {str(e)}"}

    @BaseTool.action(
        'Cancel an order',
        param_model=CancelOrderParams
    )
    async def cancel_order(self, params: CancelOrderParams) -> Dict[str, Any]:
        """Cancel an open order via authenticated CLOB API."""
        await self.ensure_initialized()
        await self.rate_limit("cancel_order")

        client, error = await self._get_authenticated_client()
        if error:
            return {"success": False, "error": error}

        try:
            # Cancel the order
            result = client.cancel_order(params.order_id)

            # Log to audit
            if self.db:
                await self.db.audit_log(
                    self._user_id,
                    "order_cancelled",
                    tool_name="polymarket",
                    details={"order_id": params.order_id}
                )

            return {
                "success": True,
                "order_id": params.order_id,
                "status": "cancelled",
                "message": "Order cancelled successfully"
            }

        except Exception as e:
            self.logger.error(f"Cancel order failed: {e}")
            return {"success": False, "error": f"Failed to cancel order: {str(e)}"}

    @BaseTool.action(
        'Cancel all orders',
        param_model=CancelAllOrdersParams
    )
    async def cancel_all_orders(self, params: CancelAllOrdersParams) -> Dict[str, Any]:
        """Cancel all open orders via authenticated CLOB API."""
        await self.ensure_initialized()
        await self.rate_limit("cancel_all_orders")

        client, error = await self._get_authenticated_client()
        if error:
            return {"success": False, "error": error}

        try:
            # Cancel all orders (optionally filtered by market)
            if params.market_id:
                result = client.cancel_market_orders(market=params.market_id)
            else:
                result = client.cancel_all()

            # Log to audit
            if self.db:
                await self.db.audit_log(
                    self._user_id,
                    "all_orders_cancelled",
                    tool_name="polymarket",
                    market_id=params.market_id,
                    details={"market_filter": params.market_id}
                )

            return {
                "success": True,
                "market_id": params.market_id,
                "status": "all_cancelled",
                "message": f"All orders cancelled{' for market ' + params.market_id if params.market_id else ''}"
            }

        except Exception as e:
            self.logger.error(f"Cancel all orders failed: {e}")
            return {"success": False, "error": f"Failed to cancel orders: {str(e)}"}
    
    # =========================================================================
    # TOOL DISCOVERY
    # =========================================================================
    
    def get_available_tools(self) -> List[Dict[str, Any]]:
        """
        Get tool list for LLM discovery.
        
        Returns list of available actions with their schemas.
        """
        if self._tools_cache is not None:
            return self._tools_cache
        
        tools = [
            # Market Discovery
            {
                "name": "search_markets",
                "description": "Search for prediction markets on Polymarket",
                "inputSchema": SearchMarketsParams.model_json_schema()
            },
            {
                "name": "get_trending_markets",
                "description": "Get trending prediction markets by 24h volume",
                "inputSchema": TrendingMarketsParams.model_json_schema()
            },
            {
                "name": "filter_markets_by_category",
                "description": "Filter markets by category (politics, sports, crypto, etc.)",
                "inputSchema": FilterByCategoryParams.model_json_schema()
            },
            {
                "name": "get_featured_markets",
                "description": "Get featured/highlighted markets",
                "inputSchema": TrendingMarketsParams.model_json_schema()
            },
            {
                "name": "get_closing_soon_markets",
                "description": "Get markets closing within specified hours",
                "inputSchema": ClosingSoonParams.model_json_schema()
            },
            {
                "name": "get_sports_markets",
                "description": "Get sports betting markets",
                "inputSchema": TrendingMarketsParams.model_json_schema()
            },
            {
                "name": "get_crypto_markets",
                "description": "Get cryptocurrency-related markets",
                "inputSchema": TrendingMarketsParams.model_json_schema()
            },
            # Market Data
            {
                "name": "get_market_details",
                "description": "Get detailed information about a specific market",
                "inputSchema": MarketDetailsParams.model_json_schema()
            },
            {
                "name": "get_current_price",
                "description": "Get current price for a market token",
                "inputSchema": GetPriceParams.model_json_schema()
            },
            {
                "name": "get_orderbook",
                "description": "Get orderbook depth for a market token",
                "inputSchema": GetOrderbookParams.model_json_schema()
            },
            {
                "name": "get_spread",
                "description": "Get bid-ask spread for a token",
                "inputSchema": GetSpreadParams.model_json_schema()
            },
            {
                "name": "get_market_volume",
                "description": "Get volume statistics for a market",
                "inputSchema": GetVolumeParams.model_json_schema()
            },
            # Portfolio (requires auth)
            {
                "name": "get_all_positions",
                "description": "Get all portfolio positions (requires trading credentials)",
                "inputSchema": GetPositionsParams.model_json_schema()
            },
            {
                "name": "get_portfolio_summary",
                "description": "Get portfolio value and P&L summary (requires trading credentials)",
                "inputSchema": PortfolioSummaryParams.model_json_schema()
            },
            {
                "name": "get_trade_history",
                "description": "Get trade history (requires trading credentials)",
                "inputSchema": GetTradeHistoryParams.model_json_schema()
            },
            # Trading (requires auth)
            {
                "name": "place_limit_order",
                "description": "Place a limit order (requires trading credentials)",
                "inputSchema": PlaceLimitOrderParams.model_json_schema()
            },
            {
                "name": "place_market_order",
                "description": "Place a marketable order for immediate fill (requires trading credentials)",
                "inputSchema": PlaceMarketOrderParams.model_json_schema()
            },
            {
                "name": "get_open_orders",
                "description": "Get open orders",
                "inputSchema": GetOpenOrdersParams.model_json_schema()
            },
            {
                "name": "get_order_history",
                "description": "Get order history",
                "inputSchema": GetOrderHistoryParams.model_json_schema()
            },
            {
                "name": "cancel_order",
                "description": "Cancel an open order (requires trading credentials)",
                "inputSchema": CancelOrderParams.model_json_schema()
            },
            {
                "name": "cancel_all_orders",
                "description": "Cancel all open orders (requires trading credentials)",
                "inputSchema": CancelAllOrdersParams.model_json_schema()
            },
            {
                "name": "get_balance",
                "description": "Get USDC balance and allowances (requires trading credentials)",
                "inputSchema": GetBalanceParams.model_json_schema()
            },
        ]

        self._tools_cache = tools
        return tools
    
    async def execute_action(self, tool_name: str, arguments: Dict[str, Any]) -> ExecutionResult:
        """
        Execute an action by name.

        Maps action names to methods for programmatic access.
        Called by API routes and agents.
        """
        start_time = time.time()
        
        # Map tool name to method
        tool_map = {
            "search_markets": (self.search_markets, SearchMarketsParams),
            "get_trending_markets": (self.get_trending_markets, TrendingMarketsParams),
            "filter_markets_by_category": (self.filter_markets_by_category, FilterByCategoryParams),
            "get_featured_markets": (self.get_featured_markets, TrendingMarketsParams),
            "get_closing_soon_markets": (self.get_closing_soon_markets, ClosingSoonParams),
            "get_sports_markets": (self.get_sports_markets, TrendingMarketsParams),
            "get_crypto_markets": (self.get_crypto_markets, TrendingMarketsParams),
            "get_market_details": (self.get_market_details, MarketDetailsParams),
            "get_current_price": (self.get_current_price, GetPriceParams),
            "get_orderbook": (self.get_orderbook, GetOrderbookParams),
            "get_spread": (self.get_spread, GetSpreadParams),
            "get_market_volume": (self.get_market_volume, GetVolumeParams),
            "get_all_positions": (self.get_all_positions, GetPositionsParams),
            "get_portfolio_summary": (self.get_portfolio_summary, PortfolioSummaryParams),
            "get_trade_history": (self.get_trade_history, GetTradeHistoryParams),
            "place_limit_order": (self.place_limit_order, PlaceLimitOrderParams),
            "place_market_order": (self.place_market_order, PlaceMarketOrderParams),
            "get_open_orders": (self.get_open_orders, GetOpenOrdersParams),
            "get_order_history": (self.get_order_history, GetOrderHistoryParams),
            "cancel_order": (self.cancel_order, CancelOrderParams),
            "cancel_all_orders": (self.cancel_all_orders, CancelAllOrdersParams),
            "get_balance": (self.get_balance, GetBalanceParams),
        }
        
        if tool_name not in tool_map:
            return ExecutionResult(
                success=False,
                error=f"Unknown tool: {tool_name}. Available: {', '.join(tool_map.keys())}",
                tool_name=tool_name
            )
        
        method, param_class = tool_map[tool_name]
        
        try:
            # Parse and validate parameters
            params = param_class(**arguments)
            
            # Execute
            result = await method(params)
            
            execution_time = (time.time() - start_time) * 1000
            
            return ExecutionResult(
                success=result.get("success", False),
                data=result,
                error=result.get("error"),
                tool_name=tool_name,
                execution_time_ms=execution_time
            )
            
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            self.logger.error(f"Tool execution failed: {tool_name}: {e}")
            return ExecutionResult(
                success=False,
                error=str(e),
                tool_name=tool_name,
                execution_time_ms=execution_time
            )



# =============================================================================
# READ / TRADE SPLIT (additive)
# =============================================================================

# Wallet-free read actions. Discovery + market data are public (Gamma/CLOB reads);
# positions/trades are public via the Data API. Order-status reads (get_open_orders/
# get_order_history) and get_balance stay on the gated trade tool (CLOB auth).
PM_READ_ACTIONS = frozenset({
    "search_markets", "get_trending_markets", "filter_markets_by_category",
    "get_featured_markets", "get_closing_soon_markets", "get_sports_markets",
    "get_crypto_markets", "get_market_details", "get_current_price", "get_orderbook",
    "get_spread", "get_market_volume", "get_all_positions", "get_portfolio_summary",
    "get_trade_history",
})


class PolymarketDataTool(PolymarketTool):
    """Read-only Polymarket tool (no wallet, delegatable). Exposes only PM_READ_ACTIONS;
    trade actions are filtered at registration, advertisement, and execution."""

    tool_id = "polymarket_data"
    name = "polymarket_data"
    description = "Polymarket prediction markets — read-only market data & research (no wallet)"

    def get_actions(self) -> Dict[str, Any]:
        return {k: v for k, v in super().get_actions().items() if k in PM_READ_ACTIONS}

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return [t for t in super().get_available_tools() if t.get("name") in PM_READ_ACTIONS]

    async def execute_action(self, tool_name: str, arguments: Dict[str, Any]):
        if tool_name not in PM_READ_ACTIONS:
            return ExecutionResult(
                success=False,
                error=f"'{tool_name}' is a trade action; use the gated 'polymarket' tool. "
                      f"'polymarket_data' is read-only.",
                tool_name=tool_name,
            )
        return await super().execute_action(tool_name, arguments)
